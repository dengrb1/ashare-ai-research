"""Standard-library configuration access for the desktop console.

The Web console and the desktop console deliberately share the same on-disk
documents, but the desktop executable must not import the application runtime.
This module therefore keeps the file format and the small amount of validation
needed by the Tk UI self contained.  It also owns atomic replacement so an
interrupted save cannot leave a half-written settings file behind.
"""

from __future__ import annotations

import copy
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

try:  # Python 3.11+ (the supported PyInstaller runtime)
    import tomllib
except ImportError:  # pragma: no cover - retained for a clearer field error
    tomllib = None  # type: ignore[assignment]


MASK = "•"
DEFAULT_GATEWAY_PORT = 8787
DEFAULT_CONTROL_API_PORT = 8000


class ConfigError(ValueError):
    """Raised when a configuration document cannot be safely loaded/saved."""


class HttpValidationError(ConfigError):
    """Raised by the UI adapter when an HTTP API rejects a payload."""


# The page order is also the source of truth for the desktop navigation.  A
# page can expose more than one settings module; modules are kept as tuples so
# the same data can be sent back to /api/settings as a partial update.
PAGE_MAPPINGS: dict[str, dict[str, Any]] = {
    "gateway": {"title": "网关监控", "modules": ()},
    "agent": {"title": "Agent 配置", "modules": ("profile", "llm", "routing")},
    "channels": {"title": "渠道中心", "modules": ("system",)},
    "subscriptions": {"title": "订阅中心", "modules": ("llm",)},
    "environment": {"title": "环境参数", "modules": ("system", "market")},
    "trading": {"title": "交易与风控", "modules": ("trading", "risk")},
    "strategy": {"title": "策略与数据", "modules": ("strategy", "market")},
    "admin": {"title": "管理面板", "modules": ("system", "prompt_cache")},
    "cockpit": {"title": "驾驶舱", "modules": ()},
}


SELECT_OPTIONS: dict[tuple[str, str], tuple[str, ...]] = {
    ("profile", "risk_level"): ("保守", "稳健", "激进"),
    ("profile", "trade_mode"): ("模拟", "实盘"),
    ("market", "kline_period"): ("1m", "5m", "15m", "30m", "1d"),
    ("market", "trading_market"): ("SH", "SZ"),
    ("system", "log_level"): ("DEBUG", "INFO", "WARNING", "ERROR"),
    ("routing", "default_effort"): ("low", "medium", "high", "xhigh", "max"),
}


CSV_FIELDS = {
    ("market", "watchlist"),
    ("llm", "news_feed_backup_url_templates"),
    ("risk", "morning_window"),
    ("risk", "afternoon_window"),
}

PAIR_FIELDS = {("risk", "morning_window"), ("risk", "afternoon_window")}

# Names used by the current schema.  These are intentionally conservative:
# unknown numeric fields are left editable as text instead of being silently
# coerced to a different type.
NUMBER_FIELDS = {
    "capital",
    "auto_trade_start_delay_seconds",
    "kline_lookback",
    "poll_interval_ms",
    "kline_refresh_seconds",
    "account_refresh_seconds",
    "quote_ttl_seconds",
    "paper_source_poll_seconds",
    "source_timeout_seconds",
    "source_max_parallel_requests",
    "source_failure_threshold",
    "source_recovery_seconds",
    "source_max_deviation_ratio",
    "session_id",
    "lot_size",
    "price_tick",
    "price_slippage_ticks",
    "order_timeout_seconds",
    "paper_initial_cash",
    "max_symbol_price",
    "max_positions",
    "max_gross_exposure",
    "max_budget",
    "max_order_volume",
    "max_order_value",
    "ai_max_order_volume",
    "max_daily_orders",
    "max_daily_loss",
    "max_price_deviation_ratio",
    "quote_max_age_seconds",
    "account_max_age_seconds",
    "order_cooldown_seconds",
    "commission_rate",
    "minimum_commission",
    "stamp_duty_rate",
    "buy_sentiment_hard_floor",
    "sentiment_max_age_seconds",
    "rsi_period",
    "fast_ma_period",
    "slow_ma_period",
    "buy_rsi_min",
    "buy_rsi_max",
    "sell_rsi",
    "buy_sentiment",
    "sell_sentiment",
    "minimum_sentiment_confidence",
    "stop_loss_ratio",
    "take_profit_ratio",
    "entry_lookback",
    "entry_max_premium_ratio",
    "poll_interval_seconds",
    "sentiment_ttl_seconds",
    "stale_grace_seconds",
    "max_articles_per_symbol",
    "max_prompt_chars",
    "news_timeout_seconds",
    "circuit_failure_threshold",
    "circuit_recovery_seconds",
    "max_parallel_requests",
    "max_remote_cost_per_day",
    "max_size",
    "default_ttl_seconds",
    "control_api_port",
    "gateway_port",
    "local_model_port",
    "quote_bridge_port",
    "news_bridge_port",
}


def infer_field_type(module: str, name: str, value: Any) -> str:
    """Return the renderer type used by the console for an existing value."""

    if module == "llm" and name == "providers":
        return "providers"
    if isinstance(value, bool):
        return "bool"
    if (module, name) in SELECT_OPTIONS:
        return "select"
    if (module, name) in PAIR_FIELDS:
        return "pair"
    if (module, name) in CSV_FIELDS:
        return "csv"
    if name in NUMBER_FIELDS or isinstance(value, (int, float)):
        return "number"
    if "key" in name.lower() or name.endswith("_secret"):
        return "secret"
    return "text"


def _deep_merge(base: Mapping[str, Any], update: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = copy.deepcopy(dict(base))
    for key, value in update.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _deep_merge(result[key], value)  # type: ignore[arg-type]
        else:
            result[key] = copy.deepcopy(value)
    return result


def mask_secret(value: str) -> str:
    """Mask a secret while retaining a short human-recognisable fingerprint."""

    value = str(value or "")
    if not value:
        return ""
    if MASK in value or "*" in value or "…" in value:
        return value
    if len(value) <= 8:
        return MASK * 5
    return f"{value[:4]}…{value[-2:]}"


def is_masked(value: Any) -> bool:
    text = str(value or "")
    return MASK in text or "*" in text or "…" in text


def preserve_secret(submitted: Any, existing: Any) -> str:
    """Keep an existing key when the form sends its masked representation."""

    candidate = str(submitted or "").strip()
    if is_masked(candidate):
        return str(existing or "")
    return candidate


def _validate_numbers(value: Any, path: str) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise ConfigError(f"{path} 必须是有限数字")
    if isinstance(value, Mapping):
        for key, child in value.items():
            _validate_numbers(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _validate_numbers(child, f"{path}[{index}]")


def validate_settings(payload: Mapping[str, Any]) -> None:
    if not isinstance(payload, Mapping):
        raise ConfigError("settings.json 根节点必须是对象")
    for module, values in payload.items():
        if module == "version":
            if not isinstance(values, (int, float)) or isinstance(values, bool):
                raise ConfigError("version 必须是数字")
            continue
        if not isinstance(values, Mapping):
            raise ConfigError(f"{module} 必须是对象")
        for name, value in values.items():
            field_type = infer_field_type(str(module), str(name), value)
            if field_type == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
                raise ConfigError(f"{module}.{name} 必须是数字")
            if field_type == "bool" and not isinstance(value, bool):
                raise ConfigError(f"{module}.{name} 必须是布尔值")
            options = SELECT_OPTIONS.get((str(module), str(name)))
            if options and str(value) not in options:
                raise ConfigError(f"{module}.{name} 不是有效选项")
            if field_type == "pair":
                parts = [part.strip() for part in str(value).split(",") if part.strip()]
                if len(parts) != 2:
                    raise ConfigError(f"{module}.{name} 需要开始,结束两个值")
        _validate_numbers(values, module)
    llm = payload.get("llm")
    if isinstance(llm, Mapping) and "providers" in llm:
        providers = llm["providers"]
        if not isinstance(providers, Sequence) or isinstance(providers, (str, bytes)):
            raise ConfigError("llm.providers 必须是数组")
        for index, provider in enumerate(providers):
            if not isinstance(provider, Mapping):
                raise ConfigError(f"llm.providers[{index}] 必须是对象")
            if not str(provider.get("name") or "").strip():
                raise ConfigError(f"llm.providers[{index}].name 不能为空")


def _toml_string(value: str) -> str:
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _toml_key(value: str) -> str:
    text = str(value)
    return text if text.replace("_", "a").replace("-", "a").isalnum() else _toml_string(text)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ConfigError("TOML 不能写入无限或 NaN 数字")
        return repr(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, Mapping):
        pairs = ", ".join(
            f"{_toml_string(str(key))} = {_toml_value(item)}" for key, item in value.items()
        )
        return "{ " + pairs + " }"
    if isinstance(value, (list, tuple)):
        return "[ " + ", ".join(_toml_value(item) for item in value) + " ]"
    if value is None:
        return _toml_string("")
    return _toml_string(str(value))


def serialize_gateway_toml(gateway: Mapping[str, Any]) -> str:
    """Serialize the structured gateway document with stable TOML tables."""

    validate_gateway(gateway)
    lines = [
        "# 由 A-Share AI Trader Desktop Console 生成。",
        "# 手动修改后可通过 POST /admin/reload 热重载。",
        "",
    ]
    channels = gateway.get("channels") or []
    scalar_keys = [key for key in gateway if key != "channels"]
    for key in scalar_keys:
        value = gateway[key]
        if value is None:
            continue
        lines.append(f"{_toml_key(str(key))} = {_toml_value(value)}")
    if scalar_keys:
        lines.append("")
    for channel in channels:
        lines.append("[[channels]]")
        for key, value in channel.items():
            if value is None or value == "" or value == [] or value == {}:
                continue
            lines.append(f"{_toml_key(str(key))} = {_toml_value(value)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def parse_gateway_toml(raw: str) -> dict[str, Any]:
    if tomllib is None:  # pragma: no cover
        raise ConfigError("当前 Python 缺少 tomllib")
    try:
        parsed = tomllib.loads(raw)
    except (tomllib.TOMLDecodeError, TypeError) as exc:
        raise ConfigError(f"gateway/config.toml 无效: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ConfigError("gateway/config.toml 根节点必须是对象")
    validate_gateway(parsed)
    return parsed


def validate_gateway(gateway: Mapping[str, Any]) -> None:
    if not isinstance(gateway, Mapping):
        raise ConfigError("网关配置必须是对象")
    if "channels" in gateway:
        channels = gateway["channels"]
        if not isinstance(channels, Sequence) or isinstance(channels, (str, bytes)):
            raise ConfigError("channels 必须是数组")
        seen: set[str] = set()
        for index, channel in enumerate(channels):
            if not isinstance(channel, Mapping):
                raise ConfigError(f"channels[{index}] 必须是对象")
            channel_id = str(channel.get("id") or "").strip()
            if not channel_id:
                raise ConfigError(f"channels[{index}].id 不能为空")
            if channel_id in seen:
                raise ConfigError(f"渠道 ID 重复: {channel_id}")
            seen.add(channel_id)
            service_type = str(channel.get("service_type") or "")
            if service_type and service_type not in {"openai", "responses", "local"}:
                raise ConfigError(f"channels[{index}].service_type 不是有效协议")
            if "priority" in channel and not isinstance(channel["priority"], (int, float)):
                raise ConfigError(f"channels[{index}].priority 必须是数字")
            for numeric_name in ("timeout_ms", "max_retries"):
                if numeric_name in channel and (isinstance(channel[numeric_name], bool) or not isinstance(channel[numeric_name], (int, float))):
                    raise ConfigError(f"channels[{index}].{numeric_name} 必须是数字")
            if "enabled" in channel and not isinstance(channel["enabled"], bool):
                raise ConfigError(f"channels[{index}].enabled 必须是布尔值")
            if "model_mapping" in channel and not isinstance(channel["model_mapping"], Mapping):
                raise ConfigError(f"channels[{index}].model_mapping 必须是对象")
            if "supported_models" in channel and not isinstance(channel["supported_models"], Sequence):
                raise ConfigError(f"channels[{index}].supported_models 必须是数组")
            if "api_keys" in channel:
                keys = channel["api_keys"]
                if not isinstance(keys, Sequence) or isinstance(keys, (str, bytes)):
                    raise ConfigError(f"channels[{index}].api_keys 必须是数组")
                if any(not isinstance(item, Mapping) for item in keys):
                    raise ConfigError(f"channels[{index}].api_keys 必须是对象数组")
    _validate_numbers(gateway, "gateway")


def add_gateway_channel(gateway: Mapping[str, Any], channel: Mapping[str, Any] | None = None) -> dict[str, Any]:
    result = copy.deepcopy(dict(gateway))
    channels = list(result.get("channels") or [])
    candidate = {
        "id": "channel-new",
        "name": "新渠道",
        "service_type": "openai",
        "base_url": "",
        "api_key_optional": True,
        "model_mapping": {},
        "supported_models": [],
        "timeout_ms": 120000,
        "max_retries": 1,
        "enabled": True,
        "priority": len(channels) + 1,
        "health_path": "/v1/models",
    }
    if channel:
        candidate.update(copy.deepcopy(dict(channel)))
    existing = {str(item.get("id")) for item in channels if isinstance(item, Mapping)}
    base = str(candidate.get("id") or "channel-new")
    channel_id = base
    suffix = 2
    while channel_id in existing:
        channel_id = f"{base}-{suffix}"
        suffix += 1
    candidate["id"] = channel_id
    channels.append(candidate)
    result["channels"] = channels
    validate_gateway(result)
    return result


def remove_gateway_channel(gateway: Mapping[str, Any], channel_id: str) -> dict[str, Any]:
    result = copy.deepcopy(dict(gateway))
    result["channels"] = [
        item for item in list(result.get("channels") or [])
        if not isinstance(item, Mapping) or str(item.get("id")) != str(channel_id)
    ]
    validate_gateway(result)
    return result


def update_gateway_channel(gateway: Mapping[str, Any], channel_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(gateway))
    updated = False
    channels: list[Any] = []
    for item in list(result.get("channels") or []):
        if isinstance(item, Mapping) and str(item.get("id")) == str(channel_id):
            merged = dict(item)
            merged.update(copy.deepcopy(dict(values)))
            channels.append(merged)
            updated = True
        else:
            channels.append(item)
    if not updated:
        raise ConfigError(f"未找到渠道: {channel_id}")
    result["channels"] = channels
    validate_gateway(result)
    return result


def atomic_write_text(path: Path, text: str) -> Path:
    """Write text beside *path*, flush it, then replace the destination."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise
    return path


def read_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"{path} 无法读取: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigError(f"{path} 根节点必须是对象")
    validate_settings(payload)
    return payload


def write_json_file(path: Path, payload: Mapping[str, Any]) -> Path:
    validate_settings(payload)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    return atomic_write_text(Path(path), text)


class ConfigStore:
    """Read/write settings and gateway documents for one repository root."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.settings_path = self.repo_root / "config" / "settings.json"
        self.gateway_path = self.repo_root / "gateway" / "config.toml"

    def read_settings(self) -> dict[str, Any]:
        return read_json_file(self.settings_path)

    def save_settings(self, payload: Mapping[str, Any], *, merge: bool = True) -> Path:
        if not isinstance(payload, Mapping):
            raise ConfigError("设置必须是对象")
        current = self.read_settings() if merge else {}
        merged = _deep_merge(current, payload)
        validate_settings(merged)
        return write_json_file(self.settings_path, merged)

    def read_gateway(self) -> dict[str, Any]:
        try:
            raw = self.gateway_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"channels": []}
        except OSError as exc:
            raise ConfigError(f"{self.gateway_path} 无法读取: {exc}") from exc
        return parse_gateway_toml(raw)

    def read_gateway_document(self) -> dict[str, Any]:
        try:
            raw = self.gateway_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raw = ""
        except OSError as exc:
            raise ConfigError(f"{self.gateway_path} 无法读取: {exc}") from exc
        parsed = parse_gateway_toml(raw) if raw.strip() else {"channels": []}
        return {"config_path": str(self.gateway_path), "raw": raw, "parsed": parsed}

    def save_gateway(self, gateway: Mapping[str, Any]) -> Path:
        return atomic_write_text(self.gateway_path, serialize_gateway_toml(gateway))

    def add_channel(self, channel: Mapping[str, Any] | None = None) -> dict[str, Any]:
        updated = add_gateway_channel(self.read_gateway(), channel)
        self.save_gateway(updated)
        return updated

    def remove_channel(self, channel_id: str) -> dict[str, Any]:
        updated = remove_gateway_channel(self.read_gateway(), channel_id)
        self.save_gateway(updated)
        return updated

    def update_channel(self, channel_id: str, values: Mapping[str, Any]) -> dict[str, Any]:
        updated = update_gateway_channel(self.read_gateway(), channel_id, values)
        self.save_gateway(updated)
        return updated

    def control_api_base(self, settings: Mapping[str, Any] | None = None) -> str:
        settings = settings if settings is not None else self.read_settings()
        system = settings.get("system") if isinstance(settings, Mapping) else {}
        system = system if isinstance(system, Mapping) else {}
        host = str(system.get("control_api_host") or "127.0.0.1").strip()
        port = _as_int(system.get("control_api_port"), DEFAULT_CONTROL_API_PORT)
        return f"http://{host}:{port}"

    def gateway_port(self, settings: Mapping[str, Any] | None = None) -> int:
        settings = settings if settings is not None else self.read_settings()
        system = settings.get("system") if isinstance(settings, Mapping) else {}
        system = system if isinstance(system, Mapping) else {}
        return _as_int(system.get("gateway_port"), DEFAULT_GATEWAY_PORT)


def _as_int(value: Any, default: int) -> int:
    try:
        number = int(value)
        return number if 1 <= number <= 65535 else default
    except (TypeError, ValueError):
        return default


__all__ = [
    "ConfigError",
    "ConfigStore",
    "DEFAULT_CONTROL_API_PORT",
    "DEFAULT_GATEWAY_PORT",
    "HttpValidationError",
    "MASK",
    "PAGE_MAPPINGS",
    "SELECT_OPTIONS",
    "add_gateway_channel",
    "atomic_write_text",
    "infer_field_type",
    "is_masked",
    "mask_secret",
    "parse_gateway_toml",
    "preserve_secret",
    "read_json_file",
    "remove_gateway_channel",
    "serialize_gateway_toml",
    "update_gateway_channel",
    "validate_gateway",
    "validate_settings",
    "write_json_file",
]
