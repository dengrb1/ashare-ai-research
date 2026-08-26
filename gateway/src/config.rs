use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};
use std::{collections::BTreeMap, env, fs, path::PathBuf};

/// Gateway 顶层配置。
///
/// 设计参照 ccx 的渠道模型：客户端模型名通过 `model_mapping` 映射到
/// 上游模型名；渠道按 `priority`（越小越优先，默认按声明顺序）依次尝试；
/// 每个渠道可配置多个 API Key（权重排序、失败冷却、按模型白名单过滤）；
/// `service_type` 决定上游协议（`openai` = /v1/chat/completions，
/// `responses` = /v1/responses，`local` = 内置安全基线）。
#[derive(Clone, Debug, Deserialize)]
#[serde(default)]
pub struct GatewayConfig {
    pub listen: String,
    /// 客户端可见的默认模型别名（/v1/models 与 /admin/status 使用）。
    pub default_model: String,
    /// 客户端鉴权密钥的环境变量名；为空表示不要求鉴权。
    pub client_api_key_env: String,
    pub max_body_bytes: usize,
    pub failure_threshold: u32,
    pub recovery_seconds: u64,
    pub health_probe_seconds: u64,
    /// 单个 Key 在 401/403 后的冷却秒数，冷却期内不再使用该 Key。
    pub key_failure_cooldown_seconds: u64,
    /// 客户端走 /v1/responses 且渠道原生端点返回 404/405/415/501 时，
    /// 是否自动降级为 /v1/chat/completions 并做协议转换。
    pub protocol_fallback: bool,
    pub channels: Vec<ChannelConfig>,
    /// 旧格式（providers 数组）兼容入口；仅在 channels 为空时使用。
    pub providers: Vec<LegacyProviderConfig>,
}

impl Default for GatewayConfig {
    fn default() -> Self {
        Self {
            listen: "127.0.0.1:8787".to_owned(),
            default_model: "ashare-advisor".to_owned(),
            client_api_key_env: "GATEWAY_CLIENT_API_KEY".to_owned(),
            max_body_bytes: 1_048_576,
            failure_threshold: 3,
            recovery_seconds: 30,
            health_probe_seconds: 20,
            key_failure_cooldown_seconds: 300,
            protocol_fallback: true,
            channels: Vec::new(),
            providers: Vec::new(),
        }
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum ServiceType {
    /// 上游是 OpenAI Chat Completions（/v1/chat/completions）。
    Openai,
    /// 上游是 OpenAI Responses（/v1/responses）。
    Responses,
    /// 内置本地安全基线（不访问网络）。
    Local,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(default)]
pub struct ChannelConfig {
    pub id: String,
    pub name: String,
    pub service_type: ServiceType,
    pub base_url: String,
    /// 环境变量覆盖 base_url（优先级高于文件里的 base_url）。
    pub base_url_env: String,
    pub api_keys: Vec<ApiKeyConfig>,
    /// 无 Key 也允许请求（本地 Ollama / llama.cpp / vLLM 等）。
    pub api_key_optional: bool,
    /// 仅 local 渠道使用：内置基线的模型名。
    pub model: String,
    /// 客户端模型名 -> 上游模型名。
    pub model_mapping: BTreeMap<String, String>,
    /// 该渠道可服务的客户端模型白名单；空 = 全部；支持通配与 ! 否定。
    pub supported_models: Vec<String>,
    pub timeout_ms: u64,
    pub max_retries: u32,
    pub enabled: bool,
    /// 数字越小越优先；相同则按声明顺序。
    pub priority: i64,
    /// 健康探测路径（相对 base_url），例如 "/models"。
    pub health_path: Option<String>,
    /// 附加到上游请求的自定义头。
    pub custom_headers: BTreeMap<String, String>,
}

impl Default for ChannelConfig {
    fn default() -> Self {
        Self {
            id: String::new(),
            name: String::new(),
            service_type: ServiceType::Openai,
            base_url: String::new(),
            base_url_env: String::new(),
            api_keys: Vec::new(),
            api_key_optional: false,
            model: String::new(),
            model_mapping: BTreeMap::new(),
            supported_models: Vec::new(),
            timeout_ms: 30_000,
            max_retries: 0,
            enabled: true,
            priority: 0,
            health_path: None,
            custom_headers: BTreeMap::new(),
        }
    }
}

#[derive(Clone, Debug, Deserialize)]
#[serde(default)]
pub struct ApiKeyConfig {
    /// 从该环境变量读取密钥（优先于内联 key）。
    pub env: String,
    /// 内联密钥（仅当 env 未设置或为空时使用）。
    pub key: String,
    /// 权重：越大越优先尝试。
    pub weight: u32,
    /// 该 Key 可服务的上游模型白名单（通配/否定）；空 = 全部。
    pub models: Vec<String>,
}

impl Default for ApiKeyConfig {
    fn default() -> Self {
        Self {
            env: String::new(),
            key: String::new(),
            weight: 1,
            models: Vec::new(),
        }
    }
}

/// 旧版 providers 配置（v0.1 格式），加载时自动迁移为 channels。
#[derive(Clone, Debug, Deserialize)]
#[serde(default)]
pub struct LegacyProviderConfig {
    pub id: String,
    pub name: String,
    pub kind: String,
    pub base_url: String,
    pub base_url_env: String,
    pub api_key_envs: Vec<String>,
    pub api_key_optional: bool,
    pub model: String,
    pub model_env: String,
    pub timeout_ms: u64,
    pub max_retries: u32,
    pub enabled: bool,
    pub health_path: Option<String>,
}

impl Default for LegacyProviderConfig {
    fn default() -> Self {
        Self {
            id: String::new(),
            name: String::new(),
            kind: "openai".to_owned(),
            base_url: String::new(),
            base_url_env: String::new(),
            api_key_envs: Vec::new(),
            api_key_optional: false,
            model: String::new(),
            model_env: String::new(),
            timeout_ms: 30_000,
            max_retries: 0,
            enabled: true,
            health_path: None,
        }
    }
}

impl GatewayConfig {
    /// 加载配置并返回配置文件路径（供 /admin/reload 使用）。
    pub fn load_with_path() -> Result<(Self, PathBuf)> {
        let explicit = env::var_os("GATEWAY_CONFIG").map(PathBuf::from);
        let selected = explicit.or_else(|| {
            [
                PathBuf::from("gateway/config.toml"),
                PathBuf::from("config.toml"),
            ]
            .into_iter()
            .find(|candidate| candidate.is_file())
        });
        let (mut config, path) = match selected {
            Some(path) => {
                let raw = fs::read_to_string(&path)
                    .with_context(|| format!("failed to read {}", path.display()))?;
                let config = toml::from_str::<Self>(&raw)
                    .with_context(|| format!("invalid gateway config {}", path.display()))?;
                (config, path)
            }
            None => (Self::default(), PathBuf::from("config.toml")),
        };
        config.normalize()?;
        Ok((config, path))
    }

    /// 从磁盘重新读取并校验配置（热重载）。
    pub fn reload_from(path: &PathBuf) -> Result<Self> {
        let raw = fs::read_to_string(path)
            .with_context(|| format!("failed to read {}", path.display()))?;
        let mut config = toml::from_str::<Self>(&raw)
            .with_context(|| format!("invalid gateway config {}", path.display()))?;
        config.normalize()?;
        Ok(config)
    }

    pub(crate) fn normalize(&mut self) -> Result<()> {
        self.listen = env::var("GATEWAY_LISTEN").unwrap_or_else(|_| self.listen.clone());
        if self.default_model.trim().is_empty() {
            bail!("default_model cannot be empty");
        }
        if self.failure_threshold == 0 || self.recovery_seconds == 0 {
            bail!("circuit settings must be positive");
        }
        if self.max_body_bytes < 1024 {
            bail!("max_body_bytes must be at least 1024");
        }
        // 旧格式迁移：providers -> channels。
        if self.channels.is_empty() && !self.providers.is_empty() {
            self.channels = self
                .providers
                .iter()
                .map(|legacy| {
                    let service_type = if legacy.kind.eq_ignore_ascii_case("local") {
                        ServiceType::Local
                    } else {
                        ServiceType::Openai
                    };
                    let mut api_keys = legacy
                        .api_key_envs
                        .iter()
                        .filter(|name| !name.is_empty())
                        .map(|name| ApiKeyConfig {
                            env: name.clone(),
                            ..ApiKeyConfig::default()
                        })
                        .collect::<Vec<_>>();
                    if api_keys.is_empty() && !legacy.model.is_empty() {
                        api_keys.push(ApiKeyConfig {
                            env: "OPENAI_API_KEY".to_owned(),
                            ..ApiKeyConfig::default()
                        });
                    }
                    let mut model_mapping = BTreeMap::new();
                    if legacy.service_type() != ServiceType::Local && !legacy.model.is_empty() {
                        model_mapping.insert(self.default_model.clone(), legacy.model.clone());
                    }
                    ChannelConfig {
                        id: legacy.id.clone(),
                        name: legacy.name.clone(),
                        service_type,
                        base_url: legacy.base_url.clone(),
                        base_url_env: legacy.base_url_env.clone(),
                        api_keys,
                        api_key_optional: legacy.api_key_optional,
                        model: legacy.model.clone(),
                        model_mapping,
                        supported_models: Vec::new(),
                        timeout_ms: legacy.timeout_ms,
                        max_retries: legacy.max_retries,
                        enabled: legacy.enabled,
                        priority: 0,
                        health_path: legacy.health_path.clone(),
                        custom_headers: BTreeMap::new(),
                    }
                })
                .collect();
        }
        if self.channels.is_empty() {
            bail!("at least one channel is required");
        }
        let mut seen = std::collections::HashSet::new();
        for channel in &mut self.channels {
            channel.resolve_env();
            channel.validate()?;
            if !seen.insert(channel.id.clone()) {
                bail!("duplicate channel id '{}'", channel.id);
            }
        }
        Ok(())
    }

    pub fn client_api_key(&self) -> Option<String> {
        env::var(&self.client_api_key_env)
            .ok()
            .map(|value| value.trim().to_owned())
            .filter(|value| !value.is_empty())
    }
}

impl LegacyProviderConfig {
    fn service_type(&self) -> ServiceType {
        if self.kind.eq_ignore_ascii_case("local") {
            ServiceType::Local
        } else {
            ServiceType::Openai
        }
    }
}

impl ChannelConfig {
    fn resolve_env(&mut self) {
        if !self.base_url_env.is_empty()
            && let Ok(value) = env::var(&self.base_url_env)
            && !value.trim().is_empty()
        {
            self.base_url = value.trim().to_owned();
        }
        self.base_url = self.base_url.trim().trim_end_matches('/').to_owned();
        self.model = self.model.trim().to_owned();
        for key in &mut self.api_keys {
            key.env = key.env.trim().to_owned();
            key.key = key.key.trim().to_owned();
            key.models = key
                .models
                .iter()
                .map(|pattern| pattern.trim().to_owned())
                .filter(|pattern| !pattern.is_empty())
                .collect();
        }
        self.model_mapping = std::mem::take(&mut self.model_mapping)
            .into_iter()
            .map(|(client, upstream)| (client.trim().to_owned(), upstream.trim().to_owned()))
            .filter(|(client, _)| !client.is_empty())
            .collect();
    }

    fn validate(&self) -> Result<()> {
        if self.id.is_empty()
            || !self
                .id
                .chars()
                .all(|value| value.is_ascii_alphanumeric() || matches!(value, '-' | '_'))
        {
            bail!("channel id must contain only ASCII letters, digits, '-' or '_'");
        }
        if self.timeout_ms == 0 {
            bail!("channel {} requires positive timeout_ms", self.id);
        }
        match self.service_type {
            ServiceType::Local => {
                if self.model.is_empty() {
                    bail!("local channel {} requires model", self.id);
                }
            }
            ServiceType::Openai | ServiceType::Responses => {
                if self.base_url.is_empty() && self.enabled && self.base_url_env.is_empty() {
                    bail!("channel {} requires base_url", self.id);
                }
                let mut has_key = self.api_key_optional;
                for key in &self.api_keys {
                    if !key.env.is_empty() || !key.key.is_empty() {
                        has_key = true;
                    }
                }
                if !has_key {
                    bail!(
                        "channel {} requires api_keys or api_key_optional = true",
                        self.id
                    );
                }
            }
        }
        Ok(())
    }

    /// 客户端模型是否可路由到该渠道。
    pub fn supports_model(&self, model: &str) -> bool {
        if self.service_type == ServiceType::Local {
            return true;
        }
        if self.supported_models.is_empty() {
            return true;
        }
        matches_model(model, &self.supported_models)
    }

    /// 客户端模型 -> 上游模型。
    pub fn upstream_model(&self, model: &str) -> String {
        self.model_mapping
            .get(model)
            .cloned()
            .unwrap_or_else(|| model.to_owned())
    }

    /// 该渠道暴露的客户端模型名集合。
    pub fn client_models(&self) -> Vec<String> {
        let mut models = self.model_mapping.keys().cloned().collect::<Vec<_>>();
        for pattern in &self.supported_models {
            let cleaned = pattern.trim_start_matches('!');
            if !cleaned.contains('*') && !models.contains(&cleaned.to_owned()) {
                models.push(cleaned.to_owned());
            }
        }
        models.sort();
        models.dedup();
        models
    }

    /// 解析后的 API Key 列表（env 优先，其次内联），None 表示无 Key。
    pub fn resolved_keys(&self) -> Vec<Option<String>> {
        let mut keys = Vec::new();
        for config in &self.api_keys {
            let value = if !config.env.is_empty() {
                env::var(&config.env)
                    .ok()
                    .map(|value| value.trim().to_owned())
                    .filter(|value| !value.is_empty())
            } else {
                Some(config.key.clone()).filter(|value| !value.is_empty())
            };
            keys.push(value);
        }
        if keys.is_empty() && self.api_key_optional {
            keys.push(None);
        }
        keys
    }
}

/// 通配模型匹配：精确、`prefix*`、`*suffix`、`*contains*`、`!` 否定。
/// 空列表放行一切。
pub fn matches_model(model: &str, patterns: &[String]) -> bool {
    let model = model.to_ascii_lowercase();
    let mut matched = false;
    let mut has_include = false;
    for raw in patterns {
        let pattern = raw.trim().to_ascii_lowercase();
        if pattern.is_empty() {
            continue;
        }
        let (negated, body) = match pattern.strip_prefix('!') {
            Some(rest) => (true, rest),
            None => (false, pattern.as_str()),
        };
        if body.is_empty() {
            continue;
        }
        let does_match = match_single_pattern(&model, body);
        if negated {
            if does_match {
                return false;
            }
            continue;
        }
        has_include = true;
        if does_match {
            matched = true;
        }
    }
    if !has_include { true } else { matched }
}

fn match_single_pattern(model: &str, pattern: &str) -> bool {
    if pattern == "*" || pattern == "**" {
        return true;
    }
    let (prefix, suffix) = match pattern.split_once('*') {
        Some((head, tail)) => (head, tail),
        None => return model == pattern,
    };
    if suffix.contains('*') {
        // *a*b* 形式：head 前缀 + tail 后缀
        let tail = suffix
            .rsplit_once('*')
            .map(|(_, last)| last)
            .unwrap_or(suffix);
        model.starts_with(prefix)
            && model.ends_with(tail)
            && model.len() >= prefix.len() + tail.len()
    } else if prefix.is_empty() {
        model.ends_with(suffix)
    } else if suffix.is_empty() {
        model.starts_with(prefix)
    } else {
        model.starts_with(prefix) && model.ends_with(suffix)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn model_wildcard_matching() {
        let patterns =
            |list: &[&str]| -> Vec<String> { list.iter().map(|value| value.to_string()).collect() };
        assert!(matches_model("gpt-4o", &patterns(&["gpt-4*"])));
        assert!(matches_model("gpt-4o-mini", &patterns(&["gpt-4*"])));
        assert!(!matches_model("claude-opus", &patterns(&["gpt-4*"])));
        assert!(matches_model("hello-world", &patterns(&["*world"])));
        assert!(matches_model("hello-world", &patterns(&["*lo-wo*"])));
        assert!(!matches_model("gpt-4o", &patterns(&["!gpt-*"])));
        assert!(matches_model(
            "claude-opus",
            &patterns(&["!gpt-*", "claude-*"])
        ));
        assert!(matches_model("anything", &patterns(&[])));
        assert!(matches_model("anything", &patterns(&["*"])));
        assert!(!matches_model("gpt-4o", &patterns(&["gpt-5*"])));
    }

    #[test]
    fn legacy_providers_migrate_to_channels() {
        let raw = r#"
listen = "127.0.0.1:8787"
route_model = "ashare-advisor"
default_model = "ashare-advisor"

[[providers]]
id = "primary"
name = "主模型"
kind = "openai"
base_url = "https://api.openai.com/v1"
api_key_envs = ["LLM_PRIMARY_API_KEY"]
model = "gpt-4.1-mini"
timeout_ms = 12000
max_retries = 1
enabled = true
health_path = "/models"
"#;
        let config: GatewayConfig = toml::from_str(raw).expect("toml");
        let mut config = config;
        config.normalize().expect("normalize");
        assert_eq!(config.channels.len(), 1);
        let channel = &config.channels[0];
        assert_eq!(channel.id, "primary");
        assert_eq!(channel.service_type, ServiceType::Openai);
        assert_eq!(channel.upstream_model("ashare-advisor"), "gpt-4.1-mini");
        assert_eq!(channel.api_keys[0].env, "LLM_PRIMARY_API_KEY");
    }
}
