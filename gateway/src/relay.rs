//! 请求中继：渠道选择、Key 池失败转移、协议转换编排。
//!
//! 流程（参照 ccx 的 handler/scheduler 语义，裁剪到两种协议）：
//! 1. 客户端协议（Chat / Responses）+ 模型名 -> 按 (priority, 声明顺序)
//!    挑选 enabled、已配置、熔断放行、supported_models 匹配的渠道；
//! 2. 每个渠道内按 Key 池顺序尝试（权重降序，401/403 进入冷却）；
//! 3. 渠道原生协议与客户端协议不一致时自动转换（请求与响应双向，
//!    流式走增量状态机）；
//! 4. 网络错误 / 408 / 429 / 5xx 视为可重试，顺序降级到下一渠道；
//!    400/422 等调用方错误立即返回，不被其它渠道掩盖；
//! 5. 客户端 /v1/responses 且渠道原生端点 404/405/415/501 时，
//!    自动降级为 /v1/chat/completions + 协议转换（protocol_fallback）。

use crate::{
    circuit::CircuitBreaker,
    config::{ChannelConfig, GatewayConfig, ServiceType},
    convert::{
        ChatToResponsesStreamer, ResponsesToChatStreamer, chat_to_responses_nonstream,
        chat_to_responses_request, responses_to_chat_nonstream, responses_to_chat_request,
    },
    keypool::KeyPool,
    metrics::{ProbeState, ProviderMetrics},
    sse,
};
use anyhow::{Context, Result};
use axum::{
    Json,
    body::{Body, Bytes},
    http::{HeaderMap, HeaderValue, StatusCode, header},
    response::{IntoResponse, Response},
};
use futures_util::{StreamExt, stream};
use serde_json::{Value, json};
use std::{
    path::PathBuf,
    sync::{
        Arc, RwLock,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
use uuid::Uuid;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ApiProtocol {
    Chat,
    Responses,
}

// =====================================================================
// 运行时
// =====================================================================

#[derive(Clone)]
pub struct ChannelRuntime {
    pub config: ChannelConfig,
    pub client: reqwest::Client,
    pub circuit: Arc<CircuitBreaker>,
    pub metrics: Arc<ProviderMetrics>,
    pub probe: Arc<ProbeState>,
    pub keypool: Arc<KeyPool>,
}

impl ChannelRuntime {
    pub fn configured(&self) -> bool {
        if !self.config.enabled {
            return false;
        }
        match self.config.service_type {
            ServiceType::Local => true,
            ServiceType::Openai | ServiceType::Responses => {
                !self.config.base_url.is_empty()
                    && (self.keypool.has_key() || self.config.api_key_optional)
            }
        }
    }
}

#[derive(Clone)]
pub struct GatewayRuntime {
    pub config: GatewayConfig,
    pub channels: Vec<ChannelRuntime>,
}

#[derive(Clone)]
pub struct AppState {
    pub runtime: Arc<RwLock<Arc<GatewayRuntime>>>,
    pub config_path: PathBuf,
    pub started_at: Instant,
    pub failovers: Arc<AtomicU64>,
}

impl AppState {
    pub fn new(config: GatewayConfig, config_path: PathBuf) -> Result<Self> {
        let runtime = build_runtime(config, None)?;
        Ok(Self {
            runtime: Arc::new(RwLock::new(Arc::new(runtime))),
            config_path,
            started_at: Instant::now(),
            failovers: Arc::new(AtomicU64::new(0)),
        })
    }

    /// 热重载配置：重新读取配置文件并重建渠道运行时；
    /// 相同 id 的渠道复用熔断器 / 指标 / 探测 / Key 池状态。
    pub fn reload(&self) -> Result<usize> {
        let config = GatewayConfig::reload_from(&self.config_path)?;
        let previous = self
            .runtime
            .read()
            .map(|rt| rt.clone())
            .unwrap_or_else(|_| {
                Arc::new(GatewayRuntime {
                    config: GatewayConfig::default(),
                    channels: Vec::new(),
                })
            });
        let runtime = build_runtime(config, Some(&previous))?;
        let count = runtime.channels.len();
        *self.runtime.write().expect("runtime lock") = Arc::new(runtime);
        Ok(count)
    }

    pub fn snapshot(&self) -> Arc<GatewayRuntime> {
        self.runtime
            .read()
            .map(|rt| rt.clone())
            .unwrap_or_else(|error| error.into_inner().clone())
    }

    pub fn configured_provider_count(&self) -> usize {
        self.snapshot()
            .channels
            .iter()
            .filter(|channel| channel.configured())
            .count()
    }

    /// 客户端可见模型集合：所有渠道映射键 + supported_models 精确项 + 默认别名。
    pub fn client_models(&self) -> Vec<String> {
        let runtime = self.snapshot();
        let mut models = std::collections::BTreeSet::new();
        models.insert(runtime.config.default_model.clone());
        for channel in &runtime.channels {
            for model in channel.config.client_models() {
                models.insert(model);
            }
        }
        models.into_iter().collect()
    }
}

fn build_runtime(
    config: GatewayConfig,
    previous: Option<&GatewayRuntime>,
) -> Result<GatewayRuntime> {
    let mut channels = Vec::with_capacity(config.channels.len());
    for channel_config in &config.channels {
        let client = reqwest::Client::builder()
            .timeout(Duration::from_millis(channel_config.timeout_ms))
            .pool_idle_timeout(Duration::from_secs(30))
            .pool_max_idle_per_host(4)
            .tcp_nodelay(true)
            .user_agent("ashare-model-gateway/0.2")
            .build()
            .with_context(|| format!("failed to build client for {}", channel_config.id))?;
        let keys = channel_config.resolved_keys();
        let weights = channel_config
            .api_keys
            .iter()
            .map(|key| key.weight)
            .collect::<Vec<_>>();
        let models = channel_config
            .api_keys
            .iter()
            .map(|key| key.models.clone())
            .collect::<Vec<_>>();
        let keypool = Arc::new(KeyPool::new(
            keys,
            weights,
            models,
            Duration::from_secs(config.key_failure_cooldown_seconds),
        ));
        // 复用既有运行时状态（按渠道 id）。
        let reused = previous.and_then(|rt| {
            rt.channels
                .iter()
                .find(|channel| channel.config.id == channel_config.id)
        });
        let circuit = reused
            .map(|channel| channel.circuit.clone())
            .unwrap_or_else(|| {
                Arc::new(CircuitBreaker::new(
                    config.failure_threshold,
                    Duration::from_secs(config.recovery_seconds),
                ))
            });
        let metrics = reused
            .map(|channel| channel.metrics.clone())
            .unwrap_or_default();
        let probe = reused
            .map(|channel| channel.probe.clone())
            .unwrap_or_default();
        if channel_config.service_type == ServiceType::Local && channel_config.enabled {
            probe.update(true, 200);
        }
        channels.push(ChannelRuntime {
            config: channel_config.clone(),
            client,
            circuit,
            metrics,
            probe,
            keypool,
        });
    }
    Ok(GatewayRuntime { config, channels })
}

/// 智能拼接上游 URL：base 已含版本段（/v1、/v1beta...）直接拼接端点，
/// 否则补 /v1；base 已以端点结尾则原样使用。
pub fn build_target_url(base_url: &str, endpoint: &str) -> String {
    let base = base_url.trim_end_matches('/');
    if base.to_ascii_lowercase().ends_with(endpoint) {
        return base.to_owned();
    }
    if has_version_suffix(base) {
        format!("{base}/{endpoint}")
    } else {
        format!("{base}/v1/{endpoint}")
    }
}

fn has_version_suffix(base: &str) -> bool {
    let Some(segment) = base.rsplit('/').next() else {
        return false;
    };
    let Some(digits) = segment.strip_prefix('v') else {
        return false;
    };
    let digits = digits.trim_end_matches(|character: char| character.is_ascii_lowercase());
    !digits.is_empty() && digits.chars().all(|character| character.is_ascii_digit())
}

// =====================================================================
// 路由
// =====================================================================

pub async fn route_request(
    state: AppState,
    headers: HeaderMap,
    mut body: Value,
    protocol: ApiProtocol,
) -> Response {
    let request_id = request_id(&headers);
    let runtime = state.snapshot();
    if !authorized(&runtime, &headers) {
        return error_response(
            StatusCode::UNAUTHORIZED,
            "UNAUTHORIZED",
            "invalid gateway credential",
            Some(&request_id),
        );
    }
    let Some(object) = body.as_object_mut() else {
        return error_response(
            StatusCode::BAD_REQUEST,
            "INVALID_REQUEST",
            "request body must be a JSON object",
            Some(&request_id),
        );
    };
    let requested_model = object
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or_default()
        .to_owned();
    if requested_model.is_empty() {
        return error_response(
            StatusCode::BAD_REQUEST,
            "INVALID_REQUEST",
            "model is required",
            Some(&request_id),
        );
    }
    if protocol == ApiProtocol::Chat && !object.get("messages").is_some_and(Value::is_array) {
        return error_response(
            StatusCode::BAD_REQUEST,
            "INVALID_REQUEST",
            "messages must be an array",
            Some(&request_id),
        );
    }
    if protocol == ApiProtocol::Responses && !object.contains_key("input") {
        return error_response(
            StatusCode::BAD_REQUEST,
            "INVALID_REQUEST",
            "input is required",
            Some(&request_id),
        );
    }
    let streaming = object
        .get("stream")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let idempotency_key = headers
        .get("idempotency-key")
        .and_then(|value| value.to_str().ok())
        .filter(|value| !value.is_empty() && value.len() <= 128)
        .map(str::to_owned)
        .unwrap_or_else(|| request_id.clone());

    // 渠道候选：按 (priority, 声明顺序) 排序。
    let mut candidates = runtime
        .channels
        .iter()
        .enumerate()
        .filter(|(_, channel)| {
            channel.configured()
                && channel.circuit.allow_request()
                && channel.config.supports_model(&requested_model)
        })
        .map(|(index, _)| index)
        .collect::<Vec<_>>();
    candidates.sort_by_key(|&index| (runtime.channels[index].config.priority, index));
    if candidates.is_empty() {
        return error_response(
            StatusCode::NOT_FOUND,
            "MODEL_NOT_FOUND",
            "no configured channel can route this model",
            Some(&request_id),
        );
    }

    let mut entering_fallback = false;
    for index in candidates {
        let channel = &runtime.channels[index];
        if entering_fallback {
            channel.metrics.record_fallback_in();
        }
        match try_channel(
            channel,
            protocol,
            &body,
            &requested_model,
            streaming,
            runtime.config.protocol_fallback,
            &request_id,
            &idempotency_key,
        )
        .await
        {
            ChannelOutcome::Delivered(response) => return response,
            ChannelOutcome::Terminal(status, error_body) => {
                return relayed_error(status, error_body, &request_id);
            }
            ChannelOutcome::Retryable => {
                entering_fallback = true;
                state.failovers.fetch_add(1, Ordering::Relaxed);
            }
        }
    }
    error_response(
        StatusCode::SERVICE_UNAVAILABLE,
        "NO_PROVIDER_AVAILABLE",
        "all configured channels are unavailable",
        Some(&request_id),
    )
}

enum ChannelOutcome {
    Delivered(Response),
    Terminal(StatusCode, Option<Bytes>),
    Retryable,
}

async fn try_channel(
    channel: &ChannelRuntime,
    protocol: ApiProtocol,
    body: &Value,
    model: &str,
    streaming: bool,
    protocol_fallback: bool,
    request_id: &str,
    idempotency_key: &str,
) -> ChannelOutcome {
    if channel.config.service_type == ServiceType::Local {
        return ChannelOutcome::Delivered(local_response(channel, protocol, streaming, request_id));
    }
    let upstream_model = channel.config.upstream_model(model);
    let candidates = channel.keypool.candidates(&upstream_model);
    if candidates.is_empty() {
        return ChannelOutcome::Retryable;
    }
    let mut last = ChannelOutcome::Retryable;
    for key_index in candidates {
        let mut attempt_no = 0u32;
        loop {
            let outcome = attempt(
                channel,
                key_index,
                protocol,
                body,
                &upstream_model,
                streaming,
                protocol_fallback,
                request_id,
                idempotency_key,
            )
            .await;
            match outcome {
                AttemptOutcome::Success(response) => {
                    channel.keypool.clear_failed(key_index);
                    return ChannelOutcome::Delivered(response);
                }
                AttemptOutcome::KeyAuthFailure => {
                    channel.keypool.mark_failed(key_index);
                    break;
                }
                AttemptOutcome::ProtocolFallback => {
                    // 客户端 Responses、渠道原生端点不支持：降级为 Chat + 转换。
                    let fallback = attempt_fallback(
                        channel,
                        key_index,
                        body,
                        &upstream_model,
                        streaming,
                        request_id,
                        idempotency_key,
                    )
                    .await;
                    match fallback {
                        AttemptOutcome::Success(response) => {
                            channel.keypool.clear_failed(key_index);
                            return ChannelOutcome::Delivered(response);
                        }
                        AttemptOutcome::KeyAuthFailure => {
                            channel.keypool.mark_failed(key_index);
                            break;
                        }
                        AttemptOutcome::ProtocolFallback => break,
                        AttemptOutcome::Retryable => break,
                        AttemptOutcome::Terminal(status, error_body) => {
                            return ChannelOutcome::Terminal(status, error_body);
                        }
                    }
                }
                AttemptOutcome::Retryable if attempt_no < channel.config.max_retries => {
                    attempt_no += 1;
                }
                AttemptOutcome::Retryable => {
                    last = ChannelOutcome::Retryable;
                    break;
                }
                AttemptOutcome::Terminal(status, error_body) => {
                    return ChannelOutcome::Terminal(status, error_body);
                }
            }
        }
    }
    last
}

enum AttemptOutcome {
    Success(Response),
    KeyAuthFailure,
    Retryable,
    ProtocolFallback,
    Terminal(StatusCode, Option<Bytes>),
}

/// 按渠道协议矩阵执行一次上游调用。
async fn attempt(
    channel: &ChannelRuntime,
    key_index: usize,
    protocol: ApiProtocol,
    body: &Value,
    upstream_model: &str,
    streaming: bool,
    protocol_fallback: bool,
    request_id: &str,
    idempotency_key: &str,
) -> AttemptOutcome {
    let (path, request_body) = match (protocol, channel.config.service_type) {
        (ApiProtocol::Chat, ServiceType::Openai) => {
            ("chat/completions", passthrough_body(body, upstream_model))
        }
        (ApiProtocol::Chat, ServiceType::Responses) => {
            ("responses", chat_to_responses_request(body, upstream_model))
        }
        (ApiProtocol::Responses, ServiceType::Openai) => (
            "chat/completions",
            responses_to_chat_request(body, upstream_model, streaming),
        ),
        (ApiProtocol::Responses, ServiceType::Responses) => {
            ("responses", passthrough_body(body, upstream_model))
        }
        (_, ServiceType::Local) => unreachable!("local channels are handled before attempt"),
    };
    send_and_handle(
        channel,
        key_index,
        path,
        &request_body,
        protocol,
        streaming,
        protocol_fallback,
        body,
        request_id,
        idempotency_key,
    )
    .await
}

/// protocol_fallback：把客户端 Responses 请求降级为 Chat 上游调用。
async fn attempt_fallback(
    channel: &ChannelRuntime,
    key_index: usize,
    body: &Value,
    upstream_model: &str,
    streaming: bool,
    request_id: &str,
    idempotency_key: &str,
) -> AttemptOutcome {
    let request_body = responses_to_chat_request(body, upstream_model, streaming);
    send_and_handle(
        channel,
        key_index,
        "chat/completions",
        &request_body,
        ApiProtocol::Responses,
        streaming,
        true,
        body,
        request_id,
        idempotency_key,
    )
    .await
}

/// 请求体透传：仅替换 model 字段。
fn passthrough_body(body: &Value, upstream_model: &str) -> Value {
    let mut value = body.clone();
    if let Some(object) = value.as_object_mut() {
        object.insert("model".to_owned(), Value::String(upstream_model.to_owned()));
    }
    value
}

/// 响应转换需求：由（客户端协议, 渠道类型, 实际调用路径）决定。
#[derive(Clone, Copy, PartialEq, Eq)]
enum ResponseConvert {
    None,
    /// 上游 Chat 响应 -> 客户端 Responses。
    ChatToResponses,
    /// 上游 Responses 响应 -> 客户端 Chat。
    ResponsesToChat,
}

fn response_convert_for(
    protocol: ApiProtocol,
    service_type: ServiceType,
    path: &str,
) -> ResponseConvert {
    if path == "chat/completions" {
        if protocol == ApiProtocol::Responses {
            ResponseConvert::ChatToResponses
        } else {
            ResponseConvert::None
        }
    } else if service_type == ServiceType::Responses {
        if protocol == ApiProtocol::Chat {
            ResponseConvert::ResponsesToChat
        } else {
            ResponseConvert::None
        }
    } else {
        ResponseConvert::None
    }
}

async fn send_and_handle(
    channel: &ChannelRuntime,
    key_index: usize,
    path: &str,
    request_body: &Value,
    protocol: ApiProtocol,
    streaming: bool,
    protocol_fallback: bool,
    original_request: &Value,
    request_id: &str,
    idempotency_key: &str,
) -> AttemptOutcome {
    let url = build_target_url(&channel.config.base_url, path);
    let key = channel.keypool.key(key_index);
    let started = channel.metrics.begin();
    let request = outbound_request(
        channel,
        key.as_deref(),
        &url,
        request_body,
        request_id,
        idempotency_key,
    );
    let response = request.send().await;
    let response = match response {
        Ok(response) => response,
        Err(_) => {
            channel.metrics.finish(started, false, true);
            channel.circuit.record_retryable_failure();
            return AttemptOutcome::Retryable;
        }
    };
    let status = response.status();
    if !status.is_success() {
        let retryable = retryable_status(status);
        let key_auth = status == StatusCode::UNAUTHORIZED || status == StatusCode::FORBIDDEN;
        channel.metrics.finish(started, false, retryable);
        if retryable {
            channel.circuit.record_retryable_failure();
            return AttemptOutcome::Retryable;
        }
        if key_auth {
            return AttemptOutcome::KeyAuthFailure;
        }
        if protocol == ApiProtocol::Responses
            && channel.config.service_type == ServiceType::Responses
            && protocol_fallback
            && protocol_unsupported_status(status)
        {
            return AttemptOutcome::ProtocolFallback;
        }
        let error_body = read_error_body(response).await;
        return AttemptOutcome::Terminal(status, error_body);
    }
    let convert = response_convert_for(protocol, channel.config.service_type, path);
    if streaming {
        handle_stream_success(
            channel,
            response,
            convert,
            started,
            original_request,
            request_id,
        )
        .await
    } else {
        handle_non_stream_success(
            channel,
            response,
            convert,
            started,
            original_request,
            request_id,
        )
        .await
    }
}

fn protocol_unsupported_status(status: StatusCode) -> bool {
    matches!(status.as_u16(), 404 | 405 | 415 | 501)
}

fn retryable_status(status: StatusCode) -> bool {
    status == StatusCode::REQUEST_TIMEOUT
        || status == StatusCode::TOO_MANY_REQUESTS
        || status.is_server_error()
}

async fn read_error_body(response: reqwest::Response) -> Option<Bytes> {
    match response.bytes().await {
        Ok(bytes) if !bytes.is_empty() => Some(bytes),
        _ => None,
    }
}

fn outbound_request<'a>(
    channel: &'a ChannelRuntime,
    key: Option<&'a str>,
    url: &'a str,
    body: &'a Value,
    request_id: &'a str,
    idempotency_key: &'a str,
) -> reqwest::RequestBuilder {
    let mut request = channel
        .client
        .post(url)
        .header(header::CONTENT_TYPE, "application/json")
        .header("x-request-id", request_id)
        .header("idempotency-key", idempotency_key)
        .json(body);
    for (name, value) in &channel.config.custom_headers {
        request = request.header(name, value);
    }
    if let Some(key) = key {
        request = request.bearer_auth(key);
    }
    request
}

// =====================================================================
// 响应处理
// =====================================================================

async fn handle_non_stream_success(
    channel: &ChannelRuntime,
    response: reqwest::Response,
    convert: ResponseConvert,
    started: Instant,
    original_request: &Value,
    request_id: &str,
) -> AttemptOutcome {
    let content_type = response.headers().get(header::CONTENT_TYPE).cloned();
    let bytes = match response.bytes().await {
        Ok(bytes) => bytes,
        Err(_) => {
            channel.metrics.finish(started, false, true);
            channel.circuit.record_retryable_failure();
            return AttemptOutcome::Retryable;
        }
    };
    channel.metrics.finish(started, true, false);
    channel.circuit.record_success();
    let client_model = original_request
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let body = match convert {
        ResponseConvert::None => Body::from(bytes),
        ResponseConvert::ChatToResponses => {
            let chat: Value = match serde_json::from_slice(&bytes) {
                Ok(chat) => chat,
                Err(_) => {
                    return AttemptOutcome::Terminal(
                        StatusCode::BAD_GATEWAY,
                        Some(Bytes::from(
                            "{\"error\":{\"message\":\"upstream chat response was not valid JSON\",\"type\":\"gateway_error\",\"code\":\"UPSTREAM_INVALID\"}}",
                        )),
                    );
                }
            };
            Body::from(chat_to_responses_nonstream(&chat, original_request).to_string())
        }
        ResponseConvert::ResponsesToChat => {
            let responses: Value = match serde_json::from_slice(&bytes) {
                Ok(responses) => responses,
                Err(_) => {
                    return AttemptOutcome::Terminal(
                        StatusCode::BAD_GATEWAY,
                        Some(Bytes::from(
                            "{\"error\":{\"message\":\"upstream responses body was not valid JSON\",\"type\":\"gateway_error\",\"code\":\"UPSTREAM_INVALID\"}}",
                        )),
                    );
                }
            };
            Body::from(responses_to_chat_nonstream(&responses, client_model).to_string())
        }
    };
    let mut response = Response::new(body);
    response.headers_mut().insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("application/json"),
    );
    let _ = content_type;
    add_gateway_headers(&mut response, channel, request_id);
    AttemptOutcome::Success(response)
}

async fn handle_stream_success(
    channel: &ChannelRuntime,
    response: reqwest::Response,
    convert: ResponseConvert,
    started: Instant,
    original_request: &Value,
    request_id: &str,
) -> AttemptOutcome {
    // 首字节锁定：第一个字节到达后即视为成功，不再降级。
    let content_type = response.headers().get(header::CONTENT_TYPE).cloned();
    let mut upstream = response.bytes_stream();
    let first = loop {
        match upstream.next().await {
            Some(Ok(bytes)) if bytes.is_empty() => continue,
            Some(Ok(bytes)) => break bytes,
            Some(Err(_)) | None => {
                channel.metrics.finish(started, false, true);
                channel.circuit.record_retryable_failure();
                return AttemptOutcome::Retryable;
            }
        }
    };
    channel.metrics.finish(started, true, false);
    channel.circuit.record_success();
    let client_model = original_request
        .get("model")
        .and_then(Value::as_str)
        .unwrap_or_default();
    let body = if convert == ResponseConvert::None {
        let combined =
            stream::once(async move { Ok::<Bytes, reqwest::Error>(first) }).chain(upstream);
        Body::from_stream(combined)
    } else {
        // 需要协议转换：SSE 逐事件转换（首字节也先喂给转换器）。
        let source =
            stream::once(async move { Ok::<Bytes, reqwest::Error>(first) }).chain(upstream);
        Body::from_stream(converted_stream(
            source,
            convert,
            client_model,
            original_request,
        ))
    };
    let mut response = Response::new(body);
    *response.status_mut() = StatusCode::OK;
    response.headers_mut().insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("text/event-stream"),
    );
    let _ = content_type;
    response
        .headers_mut()
        .insert(header::CACHE_CONTROL, HeaderValue::from_static("no-cache"));
    add_gateway_headers(&mut response, channel, request_id);
    AttemptOutcome::Success(response)
}

enum StreamerKind {
    ChatToResponses(ChatToResponsesStreamer),
    ResponsesToChat(ResponsesToChatStreamer),
}

/// SSE 协议转换流：增量解析上游 SSE，输出转换后的 SSE 行。
fn converted_stream<S>(
    source: S,
    convert: ResponseConvert,
    client_model: &str,
    original_request: &Value,
) -> impl futures_util::Stream<Item = Result<Bytes, reqwest::Error>> + 'static
where
    S: futures_util::Stream<Item = Result<Bytes, reqwest::Error>> + 'static,
{
    struct ConvertState<S> {
        source: std::pin::Pin<Box<S>>,
        streamer: StreamerKind,
        parser: sse::SseParser,
        finished: bool,
    }
    let state = ConvertState {
        source: Box::pin(source),
        streamer: match convert {
            ResponseConvert::ChatToResponses => StreamerKind::ChatToResponses(
                ChatToResponsesStreamer::new(original_request.clone()),
            ),
            ResponseConvert::ResponsesToChat => {
                StreamerKind::ResponsesToChat(ResponsesToChatStreamer::new(client_model.to_owned()))
            }
            ResponseConvert::None => unreachable!("converted_stream only for conversions"),
        },
        parser: sse::SseParser::new(),
        finished: false,
    };
    stream::unfold(state, |mut state| async move {
        if state.finished {
            return None;
        }
        loop {
            match state.source.next().await {
                Some(Ok(bytes)) => {
                    let events = state.parser.parse(&bytes);
                    let mut out = Vec::new();
                    let mut ended = false;
                    for event in events {
                        if event.is_done() {
                            ended = true;
                            match &mut state.streamer {
                                StreamerKind::ChatToResponses(streamer) => {
                                    out.extend(streamer.finish());
                                }
                                StreamerKind::ResponsesToChat(streamer) => {
                                    out.extend(streamer.finish());
                                }
                            }
                            break;
                        }
                        match &mut state.streamer {
                            StreamerKind::ChatToResponses(streamer) => {
                                if let Ok(chunk) = serde_json::from_str::<Value>(&event.data) {
                                    out.extend(streamer.feed(&chunk));
                                }
                            }
                            StreamerKind::ResponsesToChat(streamer) => {
                                out.extend(streamer.feed(&event));
                            }
                        }
                    }
                    if ended {
                        state.finished = true;
                    }
                    if out.is_empty() {
                        if ended {
                            return None;
                        }
                        continue;
                    }
                    return Some((Ok(Bytes::from(out.concat())), state));
                }
                Some(Err(_)) => {
                    // 中途断流：直接关闭（与首字节锁定语义一致）。
                    return None;
                }
                None => {
                    state.finished = true;
                    let mut out = Vec::new();
                    match &mut state.streamer {
                        StreamerKind::ChatToResponses(streamer) => out.extend(streamer.finish()),
                        StreamerKind::ResponsesToChat(streamer) => out.extend(streamer.finish()),
                    }
                    if out.is_empty() {
                        return None;
                    }
                    return Some((Ok(Bytes::from(out.concat())), state));
                }
            }
        }
    })
}

fn add_gateway_headers(response: &mut Response, channel: &ChannelRuntime, request_id: &str) {
    if let Ok(value) = HeaderValue::from_str(&channel.config.id) {
        response.headers_mut().insert("x-gateway-provider", value);
    }
    if let Ok(value) = HeaderValue::from_str(request_id) {
        response.headers_mut().insert("x-request-id", value);
    }
}

// =====================================================================
// 内置安全基线（local 渠道）
// =====================================================================

fn local_response(
    channel: &ChannelRuntime,
    protocol: ApiProtocol,
    streaming: bool,
    request_id: &str,
) -> Response {
    let started = Instant::now();
    let text = "外部模型当前不可用，本地安全基线已接管。该基线只维持服务连续性，不生成买卖指令；请结合风控状态与人工复核。";
    let model = &channel.config.model;
    let response = if streaming {
        let body = match protocol {
            ApiProtocol::Chat => {
                let chunk = json!({"id": format!("chatcmpl-{request_id}"), "object":"chat.completion.chunk", "created":unix_seconds(), "model":model, "choices":[{"index":0,"delta":{"role":"assistant","content":text},"finish_reason":"stop"}]});
                format!("data: {}\n\ndata: [DONE]\n\n", chunk)
            }
            ApiProtocol::Responses => {
                let delta = json!({"type":"response.output_text.delta","delta":text});
                let done = json!({"type":"response.completed","response":{"id":format!("resp-{request_id}"),"status":"completed","model":model}});
                format!(
                    "event: response.output_text.delta\ndata: {}\n\nevent: response.completed\ndata: {}\n\n",
                    delta, done
                )
            }
        };
        let mut response = Response::new(Body::from(body));
        response.headers_mut().insert(
            header::CONTENT_TYPE,
            HeaderValue::from_static("text/event-stream"),
        );
        response
            .headers_mut()
            .insert(header::CACHE_CONTROL, HeaderValue::from_static("no-cache"));
        response
    } else {
        let body = match protocol {
            ApiProtocol::Chat => json!({
                "id": format!("chatcmpl-{request_id}"), "object": "chat.completion", "created": unix_seconds(), "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            }),
            ApiProtocol::Responses => json!({
                "id": format!("resp-{request_id}"), "object": "response", "created_at": unix_seconds(), "status": "completed", "model": model,
                "output": [{"id":format!("msg-{request_id}"),"type":"message","role":"assistant","status":"completed","content":[{"type":"output_text","text":text,"annotations":[]}]}],
                "output_text": text, "usage":{"input_tokens":0,"output_tokens":0,"total_tokens":0}
            }),
        };
        Json(body).into_response()
    };
    channel.metrics.finish(started, true, false);
    channel.circuit.record_success();
    let mut response = response;
    add_gateway_headers(&mut response, channel, request_id);
    response
}

// =====================================================================
// 鉴权与通用工具
// =====================================================================

pub fn authorized(runtime: &GatewayRuntime, headers: &HeaderMap) -> bool {
    let Some(expected) = runtime.config.client_api_key() else {
        return true;
    };
    let supplied = headers
        .get(header::AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.strip_prefix("Bearer "))
        .or_else(|| {
            headers
                .get("x-api-key")
                .and_then(|value| value.to_str().ok())
        })
        .unwrap_or_default();
    constant_time_equal(expected.as_bytes(), supplied.as_bytes())
}

fn constant_time_equal(expected: &[u8], supplied: &[u8]) -> bool {
    let mut difference = expected.len() ^ supplied.len();
    let width = expected.len().max(supplied.len());
    for index in 0..width {
        let left = expected.get(index).copied().unwrap_or(0);
        let right = supplied.get(index).copied().unwrap_or(0);
        difference |= usize::from(left ^ right);
    }
    difference == 0
}

pub fn request_id(headers: &HeaderMap) -> String {
    headers
        .get("x-request-id")
        .and_then(|value| value.to_str().ok())
        .filter(|value| {
            !value.is_empty()
                && value.len() <= 64
                && value.chars().all(|character| {
                    character.is_ascii_alphanumeric() || matches!(character, '-' | '_' | '.')
                })
        })
        .map(str::to_owned)
        .unwrap_or_else(|| Uuid::new_v4().to_string())
}

pub fn error_response(
    status: StatusCode,
    code: &str,
    message: &str,
    request_id: Option<&str>,
) -> Response {
    let mut response = Json(json!({
        "error": {"message": message, "type": "gateway_error", "code": code},
        "request_id": request_id
    }))
    .into_response();
    *response.status_mut() = status;
    if let Some(request_id) = request_id
        && let Ok(value) = HeaderValue::from_str(request_id)
    {
        response.headers_mut().insert("x-request-id", value);
    }
    response
}

/// 透传上游终端错误响应体（保留上游错误细节）。
fn relayed_error(status: StatusCode, error_body: Option<Bytes>, request_id: &str) -> Response {
    let body = error_body.unwrap_or_else(|| {
        Bytes::from(
            json!({"error": {"message": "upstream rejected the request", "type": "gateway_error", "code": "UPSTREAM_REJECTED"}})
                .to_string(),
        )
    });
    let mut response = Response::new(Body::from(body));
    *response.status_mut() = status;
    response.headers_mut().insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("application/json"),
    );
    if let Ok(value) = HeaderValue::from_str(request_id) {
        response.headers_mut().insert("x-request-id", value);
    }
    response
}

pub fn unix_seconds() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn target_url_building() {
        assert_eq!(
            build_target_url("https://api.openai.com/v1", "chat/completions"),
            "https://api.openai.com/v1/chat/completions"
        );
        assert_eq!(
            build_target_url("https://api.openai.com/v1", "responses"),
            "https://api.openai.com/v1/responses"
        );
        assert_eq!(
            build_target_url("https://host/v1beta", "responses"),
            "https://host/v1beta/responses"
        );
        assert_eq!(
            build_target_url("https://host/chat/completions", "chat/completions"),
            "https://host/chat/completions"
        );
        assert_eq!(
            build_target_url("https://ollama.local", "chat/completions"),
            "https://ollama.local/v1/chat/completions"
        );
        assert_eq!(
            build_target_url("https://host/v2alpha", "responses"),
            "https://host/v2alpha/responses"
        );
    }

    #[test]
    fn status_classification() {
        assert!(retryable_status(StatusCode::TOO_MANY_REQUESTS));
        assert!(retryable_status(StatusCode::BAD_GATEWAY));
        assert!(!retryable_status(StatusCode::BAD_REQUEST));
        assert!(!retryable_status(StatusCode::UNAUTHORIZED));
        assert!(protocol_unsupported_status(StatusCode::NOT_FOUND));
        assert!(protocol_unsupported_status(StatusCode::METHOD_NOT_ALLOWED));
        assert!(!protocol_unsupported_status(StatusCode::BAD_REQUEST));
        assert!(!protocol_unsupported_status(
            StatusCode::UNPROCESSABLE_ENTITY
        ));
    }
}
