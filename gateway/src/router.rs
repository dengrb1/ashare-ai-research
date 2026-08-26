use crate::{
    config::ServiceType,
    relay::{ApiProtocol, AppState, authorized, error_response, route_request, unix_seconds},
};
use axum::{
    Json, Router,
    extract::State,
    http::{HeaderMap, HeaderValue, StatusCode, header},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use serde::Serialize;
use serde_json::{Value, json};
use std::{sync::atomic::Ordering, time::Duration};
use tower_http::{limit::RequestBodyLimitLayer, trace::TraceLayer};

pub fn build_router(state: AppState) -> Router {
    let body_limit = state.snapshot().config.max_body_bytes;
    Router::new()
        .route("/", get(root_info))
        .route("/health/live", get(live))
        .route("/health/ready", get(ready))
        .route("/admin/status", get(status))
        .route("/admin/reload", post(reload))
        .route("/metrics", get(metrics))
        .route("/v1/models", get(models))
        .route("/v1/chat/completions", post(chat_completions))
        .route("/v1/responses", post(responses))
        .layer(RequestBodyLimitLayer::new(body_limit))
        .layer(TraceLayer::new_for_http())
        .with_state(state)
}

/// 根路径信息页：浏览器直接打开端口时返回服务说明，避免裸 404。
async fn root_info() -> impl IntoResponse {
    Json(json!({
        "service": "ashare-model-gateway",
        "version": env!("CARGO_PKG_VERSION"),
        "description": "OpenAI Chat Completions / Responses 双协议模型网关",
        "endpoints": [
            "GET  /health/live",
            "GET  /health/ready",
            "GET  /admin/status",
            "POST /admin/reload",
            "GET  /metrics",
            "GET  /v1/models",
            "POST /v1/chat/completions",
            "POST /v1/responses"
        ]
    }))
}

pub fn spawn_health_probes(state: AppState) {
    tokio::spawn(async move {
        let probe_seconds = state.snapshot().config.health_probe_seconds.max(5);
        let mut interval = tokio::time::interval(Duration::from_secs(probe_seconds));
        loop {
            interval.tick().await;
            let runtime = state.snapshot();
            for channel in runtime.channels.iter() {
                if channel.config.service_type == ServiceType::Local || !channel.configured() {
                    continue;
                }
                let Some(path) = channel.config.health_path.as_deref() else {
                    continue;
                };
                let url = crate::relay::build_target_url(&channel.config.base_url, path);
                let mut request = channel.client.get(url);
                // 健康探测使用池内第一个可用 Key（如有）。
                let upstream_model = channel.config.upstream_model(&runtime.config.default_model);
                let candidates = channel.keypool.candidates(&upstream_model);
                if let Some(&key_index) = candidates.first()
                    && let Some(key) = channel.keypool.key(key_index)
                {
                    request = request.bearer_auth(key.as_ref());
                }
                let result = request.send().await;
                match result {
                    Ok(response) => channel
                        .probe
                        .update(response.status().is_success(), response.status().as_u16()),
                    Err(_) => channel.probe.update(false, 0),
                }
            }
        }
    });
}

async fn live() -> impl IntoResponse {
    Json(json!({"status": "ok", "service": "ashare-model-gateway"}))
}

async fn ready(State(state): State<AppState>) -> Response {
    let configured = state.configured_provider_count();
    if configured > 0 {
        (
            StatusCode::OK,
            Json(json!({"status": "ready", "providers": configured})),
        )
            .into_response()
    } else {
        error_response(
            StatusCode::SERVICE_UNAVAILABLE,
            "NO_PROVIDER",
            "no configured model channel",
            None,
        )
    }
}

#[derive(Serialize)]
struct GatewayStatus {
    service: &'static str,
    version: &'static str,
    route_model: String,
    uptime_seconds: u64,
    failovers: u64,
    auth_required: bool,
    protocol_fallback: bool,
    channels: Vec<ChannelStatus>,
    /// Web 控制台兼容视图（保留旧字段名）。
    providers: Vec<ProviderStatus>,
}

#[derive(Serialize)]
struct ChannelStatus {
    id: String,
    name: String,
    service_type: ServiceType,
    model: String,
    base_url: String,
    enabled: bool,
    configured: bool,
    priority: i64,
    key_count: usize,
    failed_keys: usize,
    model_mapping: std::collections::BTreeMap<String, String>,
    supported_models: Vec<String>,
    probe_healthy: bool,
    probe_checked_at: u64,
    probe_status_code: u64,
    circuit: crate::circuit::CircuitSnapshot,
    metrics: crate::metrics::MetricsSnapshot,
}

#[derive(Serialize)]
struct ProviderStatus {
    id: String,
    name: String,
    kind: String,
    model: String,
    base_url: String,
    enabled: bool,
    configured: bool,
    probe_healthy: bool,
    probe_checked_at: u64,
    probe_status_code: u64,
    circuit: crate::circuit::CircuitSnapshot,
    metrics: crate::metrics::MetricsSnapshot,
}

async fn status(State(state): State<AppState>) -> Json<GatewayStatus> {
    let runtime = state.snapshot();
    let default_model = runtime.config.default_model.clone();
    let mut channels = Vec::with_capacity(runtime.channels.len());
    let mut providers = Vec::with_capacity(runtime.channels.len());
    for channel in &runtime.channels {
        let model = display_model(channel, &default_model);
        let probe_healthy = channel.probe.healthy.load(Ordering::Relaxed);
        let probe_checked_at = channel.probe.checked_at.load(Ordering::Relaxed);
        let probe_status_code = channel.probe.status_code.load(Ordering::Relaxed);
        let circuit = channel.circuit.snapshot();
        let metrics = channel.metrics.snapshot();
        let configured = channel.configured();
        let service_type = channel.config.service_type;
        channels.push(ChannelStatus {
            id: channel.config.id.clone(),
            name: channel.config.name.clone(),
            service_type,
            model: model.clone(),
            base_url: channel.config.base_url.clone(),
            enabled: channel.config.enabled,
            configured,
            priority: channel.config.priority,
            key_count: channel.config.api_keys.len(),
            failed_keys: channel.keypool.failed_count(),
            model_mapping: channel.config.model_mapping.clone(),
            supported_models: channel.config.supported_models.clone(),
            probe_healthy,
            probe_checked_at,
            probe_status_code,
            circuit: circuit.clone(),
            metrics: metrics.clone(),
        });
        providers.push(ProviderStatus {
            id: channel.config.id.clone(),
            name: channel.config.name.clone(),
            kind: match service_type {
                ServiceType::Openai => "openai".to_owned(),
                ServiceType::Responses => "responses".to_owned(),
                ServiceType::Local => "local".to_owned(),
            },
            model,
            base_url: channel.config.base_url.clone(),
            enabled: channel.config.enabled,
            configured,
            probe_healthy,
            probe_checked_at,
            probe_status_code,
            circuit,
            metrics,
        });
    }
    Json(GatewayStatus {
        service: "ashare-model-gateway",
        version: env!("CARGO_PKG_VERSION"),
        route_model: default_model,
        uptime_seconds: state.started_at.elapsed().as_secs(),
        failovers: state.failovers.load(Ordering::Relaxed),
        auth_required: runtime.config.client_api_key().is_some(),
        protocol_fallback: runtime.config.protocol_fallback,
        channels,
        providers,
    })
}

fn display_model(channel: &crate::relay::ChannelRuntime, default_model: &str) -> String {
    if channel.config.service_type == ServiceType::Local {
        return channel.config.model.clone();
    }
    channel
        .config
        .model_mapping
        .get(default_model)
        .cloned()
        .or_else(|| channel.config.model_mapping.values().next().cloned())
        .unwrap_or_else(|| default_model.to_owned())
}

async fn reload(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let runtime = state.snapshot();
    if !authorized(&runtime, &headers) {
        return error_response(
            StatusCode::UNAUTHORIZED,
            "UNAUTHORIZED",
            "invalid gateway credential",
            None,
        );
    }
    match state.reload() {
        Ok(count) => Json(json!({"ok": true, "channels": count})).into_response(),
        Err(error) => error_response(
            StatusCode::BAD_REQUEST,
            "RELOAD_FAILED",
            &error.to_string(),
            None,
        ),
    }
}

async fn metrics(State(state): State<AppState>) -> Response {
    let runtime = state.snapshot();
    let mut output = String::from(
        "# HELP ashare_gateway_failovers_total Ordered channel failovers\n\
         # TYPE ashare_gateway_failovers_total counter\n",
    );
    output.push_str(&format!(
        "ashare_gateway_failovers_total {}\n",
        state.failovers.load(Ordering::Relaxed)
    ));
    for channel in runtime.channels.iter() {
        let snapshot = channel.metrics.snapshot();
        let id = &channel.config.id;
        output.push_str(&format!(
            "ashare_gateway_channel_requests_total{{channel=\"{id}\"}} {}\n",
            snapshot.requests
        ));
        output.push_str(&format!(
            "ashare_gateway_channel_errors_total{{channel=\"{id}\"}} {}\n",
            snapshot.errors
        ));
        output.push_str(&format!(
            "ashare_gateway_channel_active{{channel=\"{id}\"}} {}\n",
            snapshot.active
        ));
        output.push_str(&format!(
            "ashare_gateway_channel_latency_ms{{channel=\"{id}\"}} {:.3}\n",
            snapshot.last_latency_ms
        ));
    }
    let mut response = output.into_response();
    response.headers_mut().insert(
        header::CONTENT_TYPE,
        HeaderValue::from_static("text/plain; version=0.0.4; charset=utf-8"),
    );
    response
}

async fn models(State(state): State<AppState>, headers: HeaderMap) -> Response {
    let runtime = state.snapshot();
    if !authorized(&runtime, &headers) {
        return error_response(
            StatusCode::UNAUTHORIZED,
            "UNAUTHORIZED",
            "invalid gateway credential",
            None,
        );
    }
    let created = unix_seconds();
    let data = state
        .client_models()
        .into_iter()
        .map(|model| {
            json!({
                "id": model,
                "object": "model",
                "created": created,
                "owned_by": "ashare-gateway"
            })
        })
        .collect::<Vec<_>>();
    Json(json!({"object": "list", "data": data})).into_response()
}

async fn chat_completions(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(body): Json<Value>,
) -> Response {
    route_request(state, headers, body, ApiProtocol::Chat).await
}

async fn responses(
    State(state): State<AppState>,
    headers: HeaderMap,
    Json(body): Json<Value>,
) -> Response {
    route_request(state, headers, body, ApiProtocol::Responses).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::{ApiKeyConfig, ChannelConfig, GatewayConfig};
    use axum::body::{Body, Bytes, to_bytes};
    use futures_util::stream;
    use std::sync::Arc;
    use std::sync::atomic::{AtomicUsize, Ordering as AtomicOrdering};
    use tower::ServiceExt;

    fn channel(id: &str, service_type: ServiceType, base_url: &str, key: &str) -> ChannelConfig {
        ChannelConfig {
            id: id.to_owned(),
            name: id.to_owned(),
            service_type,
            base_url: base_url.to_owned(),
            api_keys: vec![ApiKeyConfig {
                key: key.to_owned(),
                ..ApiKeyConfig::default()
            }],
            ..ChannelConfig::default()
        }
    }

    fn test_config(channels: Vec<ChannelConfig>) -> GatewayConfig {
        let mut config = GatewayConfig {
            default_model: "ashare-advisor".to_owned(),
            channels,
            ..GatewayConfig::default()
        };
        config.normalize().expect("config normalize");
        config
    }

    async fn spawn_stub(status: StatusCode, calls: Arc<AtomicUsize>, name: &'static str) -> String {
        let app = Router::new().route(
            "/v1/chat/completions",
            post(|State(state): State<(StatusCode, Arc<AtomicUsize>, &'static str)>| async move {
                state.1.fetch_add(1, AtomicOrdering::SeqCst);
                if !state.0.is_success() {
                    return (
                        state.0,
                        Json(json!({"error": {"message": "stub", "type": "stub_error"}})),
                    )
                        .into_response();
                }
                Json(json!({
                    "id": format!("chatcmpl-{}", state.2),
                    "object": "chat.completion",
                    "model": "stub-model",
                    "choices": [{"index":0,"message":{"role":"assistant","content":state.2},"finish_reason":"stop"}],
                    "usage": {"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}
                }))
                .into_response()
            }),
        )
        .with_state((status, calls, name));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("stub listener");
        let address = listener.local_addr().expect("stub address");
        tokio::spawn(async move {
            axum::serve(listener, app).await.expect("stub server");
        });
        format!("http://{address}/v1")
    }

    /// 回显收到的请求体（用于断言上游模型名与转换结果）。
    async fn spawn_echo_stub() -> String {
        let app = Router::new().route(
            "/v1/chat/completions",
            post(|body: Json<Value>| async move {
                Json(json!({
                    "id": "chatcmpl-echo",
                    "object": "chat.completion",
                    "model": body.get("model"),
                    "choices": [{"index":0,"message":{"role":"assistant","content":serde_json::to_string(&body.0).unwrap()},"finish_reason":"stop"}],
                    "usage": {"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}
                }))
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
            .await
            .expect("echo listener");
        let address = listener.local_addr().expect("echo address");
        tokio::spawn(async move {
            axum::serve(listener, app).await.expect("echo server");
        });
        format!("http://{address}/v1")
    }

    fn spawn_state(channels: Vec<ChannelConfig>) -> AppState {
        AppState::new(
            test_config(channels),
            std::path::PathBuf::from("test-config.toml"),
        )
        .expect("state")
    }

    fn completion_request(model: &str) -> axum::http::Request<Body> {
        axum::http::Request::builder()
            .method("POST")
            .uri("/v1/chat/completions")
            .header(header::CONTENT_TYPE, "application/json")
            .body(Body::from(format!(
                r#"{{"model":"{model}","messages":[{{"role":"user","content":"status"}}]}}"#
            )))
            .expect("request")
    }

    fn responses_request(model: &str, stream: bool) -> axum::http::Request<Body> {
        axum::http::Request::builder()
            .method("POST")
            .uri("/v1/responses")
            .header(header::CONTENT_TYPE, "application/json")
            .body(Body::from(format!(
                r#"{{"model":"{model}","input":"status","stream":{stream}}}"#
            )))
            .expect("responses request")
    }

    #[tokio::test]
    async fn chat_passthrough_applies_model_mapping() {
        let base_url = spawn_echo_stub().await;
        let mut channel = channel("primary", ServiceType::Openai, &base_url, "test-key");
        channel
            .model_mapping
            .insert("ashare-advisor".to_owned(), "gpt-stub".to_owned());
        let app = build_router(spawn_state(vec![channel]));
        let response = app
            .oneshot(completion_request("ashare-advisor"))
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(
            response.headers().get("x-gateway-provider").unwrap(),
            "primary"
        );
        let bytes = to_bytes(response.into_body(), 256 * 1024).await.unwrap();
        let payload: Value = serde_json::from_slice(&bytes).unwrap();
        // 回显的请求体应包含映射后的模型名。
        let echoed: Value = serde_json::from_str(
            payload["choices"][0]["message"]["content"]
                .as_str()
                .unwrap(),
        )
        .unwrap();
        assert_eq!(echoed["model"], "gpt-stub");
    }

    #[tokio::test]
    async fn responses_protocol_converts_via_openai_channel() {
        let calls = Arc::new(AtomicUsize::new(0));
        let base_url = spawn_stub(StatusCode::OK, calls.clone(), "compat").await;
        let app = build_router(spawn_state(vec![channel(
            "compat",
            ServiceType::Openai,
            &base_url,
            "test-key",
        )]));
        let response = app
            .oneshot(responses_request("ashare-advisor", false))
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let bytes = to_bytes(response.into_body(), 256 * 1024).await.unwrap();
        let payload: Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(payload["object"], "response");
        assert_eq!(payload["status"], "completed");
        assert_eq!(payload["output_text"], "compat");
        assert_eq!(payload["usage"]["total_tokens"], 2);
        assert_eq!(calls.load(AtomicOrdering::SeqCst), 1);
    }

    /// Chat 客户端 + responses 上游：请求转 responses、响应转回 chat。
    #[tokio::test]
    async fn chat_protocol_converts_via_responses_channel() {
        let app = Router::new().route(
            "/v1/responses",
            post(|body: Json<Value>| async move {
                // 断言上游收到转换后的 responses 请求。
                assert!(body.get("input").is_some());
                Json(json!({
                    "id": "resp_1",
                    "object": "response",
                    "created_at": 100,
                    "status": "completed",
                    "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "来自responses上游"}]}],
                    "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7}
                }))
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        let base_url = format!("http://{address}/v1");
        let app = build_router(spawn_state(vec![channel(
            "resp-upstream",
            ServiceType::Responses,
            &base_url,
            "test-key",
        )]));
        let response = app
            .oneshot(completion_request("ashare-advisor"))
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let bytes = to_bytes(response.into_body(), 256 * 1024).await.unwrap();
        let payload: Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(payload["object"], "chat.completion");
        assert_eq!(
            payload["choices"][0]["message"]["content"],
            "来自responses上游"
        );
        assert_eq!(payload["usage"]["total_tokens"], 7);
    }

    /// Key 池：第一个 Key 401 后切换到第二个 Key。
    #[tokio::test]
    async fn key_pool_fails_over_on_auth_error() {
        let app = Router::new().route(
            "/v1/chat/completions",
            post(|headers: HeaderMap| async move {
                let key = headers
                    .get(header::AUTHORIZATION)
                    .and_then(|value| value.to_str().ok())
                    .unwrap_or_default();
                if key.contains("bad-key") {
                    (
                        StatusCode::UNAUTHORIZED,
                        Json(json!({"error": {"message": "invalid key"}})),
                    )
                        .into_response()
                } else {
                    Json(json!({
                        "id": "chatcmpl-good",
                        "object": "chat.completion",
                        "model": "m",
                        "choices": [{"index":0,"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}],
                        "usage": {"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}
                    }))
                    .into_response()
                }
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        let base_url = format!("http://{address}/v1");
        let mut channel = channel("pool", ServiceType::Openai, &base_url, "good-key");
        channel.api_keys = vec![
            ApiKeyConfig {
                key: "bad-key".to_owned(),
                ..ApiKeyConfig::default()
            },
            ApiKeyConfig {
                key: "good-key".to_owned(),
                ..ApiKeyConfig::default()
            },
        ];
        let state = spawn_state(vec![channel]);
        let app = build_router(state.clone());
        let response = app
            .oneshot(completion_request("ashare-advisor"))
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let bytes = to_bytes(response.into_body(), 256 * 1024).await.unwrap();
        let payload: Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(payload["choices"][0]["message"]["content"], "ok");
        // bad-key 进入冷却。
        let runtime = state.snapshot();
        assert_eq!(runtime.channels[0].keypool.failed_count(), 1);
    }

    /// 调用方错误（400）直接返回，不被其它渠道掩盖。
    #[tokio::test]
    async fn caller_error_is_terminal() {
        let primary_calls = Arc::new(AtomicUsize::new(0));
        let secondary_calls = Arc::new(AtomicUsize::new(0));
        let primary_url =
            spawn_stub(StatusCode::BAD_REQUEST, primary_calls.clone(), "primary").await;
        let secondary_url = spawn_stub(StatusCode::OK, secondary_calls.clone(), "secondary").await;
        let app = build_router(spawn_state(vec![
            channel("primary", ServiceType::Openai, &primary_url, "k"),
            channel("secondary", ServiceType::Openai, &secondary_url, "k"),
        ]));
        let response = app
            .oneshot(completion_request("ashare-advisor"))
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::BAD_REQUEST);
        assert_eq!(primary_calls.load(AtomicOrdering::SeqCst), 1);
        assert_eq!(secondary_calls.load(AtomicOrdering::SeqCst), 0);
    }

    /// 渠道原生 /v1/responses 返回 404 时自动降级为 chat 转换。
    #[tokio::test]
    async fn protocol_fallback_on_404() {
        let app = Router::new()
            .route(
                "/v1/responses",
                post(|| async {
                    (
                        StatusCode::NOT_FOUND,
                        Json(json!({"error": {"message": "responses unsupported"}})),
                    )
                        .into_response()
                }),
            )
            .route(
                "/v1/chat/completions",
                post(|| async {
                    Json(json!({
                        "id": "chatcmpl-fallback",
                        "object": "chat.completion",
                        "model": "m",
                        "choices": [{"index":0,"message":{"role":"assistant","content":"chat回答"},"finish_reason":"stop"}],
                        "usage": {"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}
                    }))
                }),
            );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        let base_url = format!("http://{address}/v1");
        let app = build_router(spawn_state(vec![channel(
            "resp-only",
            ServiceType::Responses,
            &base_url,
            "k",
        )]));
        let response = app
            .oneshot(responses_request("ashare-advisor", false))
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let bytes = to_bytes(response.into_body(), 256 * 1024).await.unwrap();
        let payload: Value = serde_json::from_slice(&bytes).unwrap();
        assert_eq!(payload["object"], "response");
        assert_eq!(payload["output_text"], "chat回答");
    }

    /// 流式：chat SSE -> responses SSE（端到端）。
    #[tokio::test]
    async fn streaming_chat_to_responses_end_to_end() {
        let app = Router::new().route(
            "/v1/chat/completions",
            post(|| async {
                let chunks = stream::iter(vec![
                    Ok::<Bytes, std::io::Error>(Bytes::from("data: {\"id\":\"chatcmpl-1\",\"choices\":[{\"index\":0,\"delta\":{\"role\":\"assistant\",\"content\":\"你好\"},\"finish_reason\":null}]}\n\n")),
                    Ok::<Bytes, std::io::Error>(Bytes::from("data: {\"id\":\"chatcmpl-1\",\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n")),
                    Ok::<Bytes, std::io::Error>(Bytes::from("data: [DONE]\n\n")),
                ]);
                let mut response = Response::new(Body::from_stream(chunks));
                response.headers_mut().insert(
                    header::CONTENT_TYPE,
                    HeaderValue::from_static("text/event-stream"),
                );
                response
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        let base_url = format!("http://{address}/v1");
        let app = build_router(spawn_state(vec![channel(
            "stream-chat",
            ServiceType::Openai,
            &base_url,
            "k",
        )]));
        let response = app
            .oneshot(responses_request("ashare-advisor", true))
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let bytes = to_bytes(response.into_body(), 1024 * 1024).await.unwrap();
        let wire = String::from_utf8(bytes.to_vec()).unwrap();
        assert!(wire.contains("event: response.created"));
        assert!(wire.contains("event: response.output_text.delta"));
        assert!(wire.contains("\"delta\":\"你好\""));
        assert!(wire.contains("event: response.completed"));
        assert!(!wire.contains("[DONE]"));
    }

    /// 流式：responses SSE -> chat SSE（端到端）。
    #[tokio::test]
    async fn streaming_responses_to_chat_end_to_end() {
        let app = Router::new().route(
            "/v1/responses",
            post(|| async {
                let chunks = stream::iter(vec![
                    Ok::<Bytes, std::io::Error>(Bytes::from("event: response.output_text.delta\ndata: {\"type\":\"response.output_text.delta\",\"delta\":\"你好\"}\n\n")),
                    Ok::<Bytes, std::io::Error>(Bytes::from("event: response.completed\ndata: {\"type\":\"response.completed\",\"response\":{\"id\":\"resp_1\",\"status\":\"completed\"},\"usage\":{\"input_tokens\":3,\"output_tokens\":4,\"total_tokens\":7}}\n\n")),
                ]);
                let mut response = Response::new(Body::from_stream(chunks));
                response.headers_mut().insert(
                    header::CONTENT_TYPE,
                    HeaderValue::from_static("text/event-stream"),
                );
                response
            }),
        );
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        tokio::spawn(async move {
            axum::serve(listener, app).await.unwrap();
        });
        let base_url = format!("http://{address}/v1");
        let app = build_router(spawn_state(vec![channel(
            "stream-resp",
            ServiceType::Responses,
            &base_url,
            "k",
        )]));
        let request = axum::http::Request::builder()
            .method("POST")
            .uri("/v1/chat/completions")
            .header(header::CONTENT_TYPE, "application/json")
            .body(Body::from(
                r#"{"model":"ashare-advisor","stream":true,"messages":[{"role":"user","content":"hi"}]}"#,
            ))
            .unwrap();
        let response = app.oneshot(request).await.unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let bytes = to_bytes(response.into_body(), 1024 * 1024).await.unwrap();
        let wire = String::from_utf8(bytes.to_vec()).unwrap();
        assert!(wire.contains("\"role\":\"assistant\""));
        assert!(wire.contains("\"content\":\"你好\""));
        assert!(wire.contains("\"finish_reason\":\"stop\""));
        assert!(wire.ends_with("data: [DONE]\n\n"));
    }

    #[tokio::test]
    async fn models_lists_client_models() {
        let calls = Arc::new(AtomicUsize::new(0));
        let base_url = spawn_stub(StatusCode::OK, calls, "m").await;
        let mut channel = channel("primary", ServiceType::Openai, &base_url, "k");
        channel
            .model_mapping
            .insert("ashare-advisor".to_owned(), "gpt-4.1-mini".to_owned());
        channel
            .model_mapping
            .insert("deepseek".to_owned(), "deepseek-chat".to_owned());
        let app = build_router(spawn_state(vec![channel]));
        let response = app
            .oneshot(
                axum::http::Request::builder()
                    .uri("/v1/models")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let bytes = to_bytes(response.into_body(), 64 * 1024).await.unwrap();
        let payload: Value = serde_json::from_slice(&bytes).unwrap();
        let ids = payload["data"]
            .as_array()
            .unwrap()
            .iter()
            .map(|model| model["id"].as_str().unwrap().to_owned())
            .collect::<Vec<_>>();
        assert!(ids.contains(&"ashare-advisor".to_owned()));
        assert!(ids.contains(&"deepseek".to_owned()));
    }

    #[tokio::test]
    async fn client_auth_enforced_when_configured() {
        let unique_env = "GATEWAY_TEST_AUTH_KEY_9F3A";
        // SAFETY: 测试使用进程内唯一的环境变量名，与并行测试无冲突。
        unsafe {
            std::env::set_var(unique_env, "secret-token");
        }
        let base_url = spawn_stub(StatusCode::OK, Arc::new(AtomicUsize::new(0)), "m").await;
        let channel = channel("primary", ServiceType::Openai, &base_url, "k");
        let mut config = test_config(vec![channel]);
        config.client_api_key_env = unique_env.to_owned();
        let state = AppState::new(config, std::path::PathBuf::from("test-config.toml")).unwrap();
        let app = build_router(state);

        // 无密钥 -> 401
        let response = app
            .clone()
            .oneshot(completion_request("ashare-advisor"))
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::UNAUTHORIZED);
        // 正确密钥 -> 200
        let request = axum::http::Request::builder()
            .method("POST")
            .uri("/v1/chat/completions")
            .header(header::CONTENT_TYPE, "application/json")
            .header(header::AUTHORIZATION, "Bearer secret-token")
            .body(Body::from(
                r#"{"model":"ashare-advisor","messages":[{"role":"user","content":"status"}]}"#,
            ))
            .unwrap();
        let response = app.oneshot(request).await.unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        // SAFETY: 清理测试环境变量。
        unsafe {
            std::env::remove_var(unique_env);
        }
    }

    /// 热重载：修改配置文件后 /admin/reload 生效。
    #[tokio::test]
    async fn admin_reload_applies_config_changes() {
        let calls = Arc::new(AtomicUsize::new(0));
        let base_url = spawn_stub(StatusCode::OK, calls, "m").await;
        let dir = std::env::temp_dir().join(format!("gateway-reload-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let config_path = dir.join("config.toml");
        let write_config = |models: &[&str]| {
            let channels = models
                .iter()
                .map(|model| {
                    format!(
                        "[[channels]]\nid = \"ch-{model}\"\nname = \"{model}\"\nservice_type = \"openai\"\nbase_url = \"{base_url}\"\napi_keys = [{{ key = \"k\" }}]\nmodel_mapping = {{ \"ashare-advisor\" = \"{model}\" }}\n"
                    )
                })
                .collect::<Vec<_>>()
                .join("\n");
            std::fs::write(
                &config_path,
                format!("default_model = \"ashare-advisor\"\n{channels}\n"),
            )
            .unwrap();
        };
        write_config(&["one"]);
        let config = GatewayConfig::reload_from(&config_path).unwrap();
        let state = AppState::new(config, config_path.clone()).unwrap();
        assert_eq!(state.snapshot().channels.len(), 1);

        write_config(&["one", "two"]);
        let app = build_router(state.clone());
        let response = app
            .oneshot(
                axum::http::Request::builder()
                    .method("POST")
                    .uri("/admin/reload")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        assert_eq!(state.snapshot().channels.len(), 2);
        std::fs::remove_dir_all(&dir).ok();
    }
}
