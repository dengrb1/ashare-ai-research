"""Validated user-owned watchlists and minute monitoring preferences."""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SYMBOL = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$")


def normalize_asset_symbol(value: str) -> str:
    value = value.strip().upper()
    if re.fullmatch(r"\d{6}", value):
        exchange = "BJ" if value.startswith(("4", "8", "92")) else "SH" if value.startswith(("5", "6")) else "SZ"
        value = f"{value}.{exchange}"
    if SYMBOL.fullmatch(value) is None:
        raise ValueError(f"无效证券代码: {value}")
    return value


def normalize_symbols(values: list[str]) -> list[str]:
    result = list(dict.fromkeys(normalize_asset_symbol(value) for value in values))
    if len(result) > 100:
        raise ValueError("自选最多支持 100 个证券代码")
    return result


class MonitoringPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    enabled: bool = True
    interval_seconds: Literal[60] = 60
    cooldown_seconds: int = Field(default=300, ge=60, le=86400)
    price_change_pct: float = Field(default=2, gt=0, le=30)
    volume_multiplier: float = Field(default=3, gt=1, le=100)
    high_severity_price_change_pct: float = Field(default=5, gt=0, le=30)
    divergence_price_pct: float = Field(default=1, gt=0, le=30)
    divergence_volume_ratio: float = Field(default=0.5, gt=0, lt=1)
    severity: Literal["WARNING", "HIGH", "CRITICAL"] = "HIGH"
    price_enabled: bool = True
    volume_enabled: bool = True
    divergence_enabled: bool = True
    position_enabled: bool = True

    @model_validator(mode="after")
    def validate_thresholds(self) -> MonitoringPolicy:
        if self.high_severity_price_change_pct < self.price_change_pct:
            raise ValueError("严重涨跌阈值不能小于普通涨跌阈值")
        return self


class WatchlistGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    group_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=64)
    sort_order: int = Field(default=0, ge=0, le=10000)
    enabled: bool = True
    symbols: list[str] = Field(default_factory=list, max_length=100)
    monitoring_policy: dict[str, Any] = Field(default_factory=dict)

    @field_validator("symbols")
    @classmethod
    def validate_symbols(cls, value: list[str]) -> list[str]:
        return normalize_symbols(value)

    @field_validator("monitoring_policy")
    @classmethod
    def validate_policy(cls, value: dict[str, Any]) -> dict[str, Any]:
        return MonitoringPolicy(**value).model_dump(exclude_unset=True)


def default_group(symbols: list[str]) -> dict[str, Any]:
    return WatchlistGroup(group_id="default", name="默认分组", symbols=symbols).model_dump()


def normalize_groups(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(groups) > 32:
        raise ValueError("最多支持 32 个分组")
    parsed = [WatchlistGroup.model_validate(group).model_dump() for group in groups]
    if len({group["group_id"] for group in parsed}) != len(parsed):
        raise ValueError("分组 ID 不能重复")
    normalize_symbols([symbol for group in parsed for symbol in group["symbols"]])
    return sorted(parsed, key=lambda item: item["sort_order"])


def flat_watchlist(groups: list[dict[str, Any]]) -> list[str]:
    # Disabled groups remain in the legacy flat contract; enabled gates monitoring.
    return normalize_symbols([symbol for group in groups for symbol in group["symbols"]])


def reconcile_legacy_watchlist(groups: list[dict[str, Any]], symbols: list[str]) -> list[dict[str, Any]]:
    desired = normalize_symbols(symbols)
    result = [{**group, "symbols": [s for s in group["symbols"] if s in desired]} for group in groups]
    existing = flat_watchlist(result)
    additions = [s for s in desired if s not in existing]
    default = next((group for group in result if group["group_id"] == "default"), None)
    if default is None:
        default = default_group([])
        result.append(default)
    default["symbols"] += additions
    return normalize_groups(result)


def parse_watchlist_text(text: str) -> dict[str, Any]:
    """Parse JSON/CSV/code lists, reporting invalid entries instead of dropping them."""
    raw: list[str] = []
    stripped = text.strip()
    if stripped.startswith(("[", "{")):
        value = json.loads(stripped)
        value = value.get("symbols", value.get("watchlist", [])) if isinstance(value, dict) else value
        if not isinstance(value, list):
            raise ValueError("JSON 必须包含代码列表")
        raw = [item.get("symbol", "") if isinstance(item, dict) else str(item) for item in value]
    else:
        for row in csv.reader(io.StringIO(stripped)):
            for cell in row:
                for token in re.split(r"[\s;，；]+", cell.strip()):
                    if not token or token.casefold() in {"symbol", "code", "name", "代码", "证券代码", "名称"}:
                        continue
                    if re.search(r"\d", token) or re.fullmatch(r"[A-Za-z.]+", token):
                        raw.append(token)
    symbols: list[str] = []
    invalid: list[str] = []
    duplicates: list[str] = []
    for item in raw:
        try:
            symbol = normalize_asset_symbol(item)
        except ValueError:
            invalid.append(item)
            continue
        if symbol in symbols:
            duplicates.append(symbol)
        else:
            symbols.append(symbol)
    normalize_symbols(symbols)
    return {"symbols": symbols, "invalid": invalid, "duplicates": duplicates, "duplicates_removed": len(duplicates)}
