from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from ashare_ai.core.contracts import Disclosure, NewsItem


class EventSeverity(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ActiveEventRisk(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    severity: EventSeverity
    trusted_source: bool
    source: str | None = None
    title: str | None = None
    published_at: datetime | None = None


class EventRiskPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    multipliers: dict[EventSeverity, float]
    blocked_severities: frozenset[EventSeverity]
    require_trusted_source_for_block: bool = True


class EventRiskDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    multiplier: float = Field(ge=0, le=1)
    block_new: bool
    policy_version: str
    event_ids: tuple[str, ...]


def aggregate_event_risk(
    events: list[ActiveEventRisk] | tuple[ActiveEventRisk, ...],
    policy: EventRiskPolicy,
) -> EventRiskDecision:
    missing = {event.severity for event in events} - set(policy.multipliers)
    if missing:
        raise ValueError(f"event policy is missing severities: {sorted(missing)}")
    blocked = any(
        event.severity in policy.blocked_severities
        and (event.trusted_source or not policy.require_trusted_source_for_block)
        for event in events
    )
    multiplier = (
        0.0
        if blocked
        else min((policy.multipliers[event.severity] for event in events), default=1.0)
    )
    if not 0 <= multiplier <= 1:
        raise ValueError("event risk multiplier must be within [0, 1]")
    return EventRiskDecision(
        multiplier=multiplier,
        block_new=blocked,
        policy_version=policy.version,
        event_ids=tuple(sorted(event.event_id for event in events)),
    )


CRITICAL_OFFICIAL_KEYWORDS = (
    "财务造假",
    "欺诈发行",
    "重大违法强制退市",
    "终止上市",
)
HIGH_OFFICIAL_KEYWORDS = (
    "立案调查",
    "重大诉讼",
    "债务违约",
    "资金占用",
    "违规担保",
    "行政处罚",
)
MEDIA_NEGATIVE_KEYWORDS = CRITICAL_OFFICIAL_KEYWORDS + HIGH_OFFICIAL_KEYWORDS + (
    "重大违法",
    "退市风险",
    "流动性危机",
    "偿债风险",
    "经营异常",
)


def classify_event_risks(
    disclosures: list[Disclosure] | tuple[Disclosure, ...],
    news: list[NewsItem] | tuple[NewsItem, ...],
    *,
    symbol: str,
    decision_at: datetime,
    window_days: int = 30,
) -> tuple[ActiveEventRisk, ...]:
    """Classify recent negative events without allowing media-only hard blocks."""

    if window_days <= 0:
        raise ValueError("window_days must be positive")
    window_start = decision_at - timedelta(days=window_days)
    result: dict[str, ActiveEventRisk] = {}

    for disclosure in disclosures:
        if disclosure.symbol != symbol:
            continue
        published_at = disclosure.published_at or disclosure.available_at
        if published_at > decision_at or published_at < window_start:
            continue
        severity = _severity_for_title(
            disclosure.title, trusted=disclosure.official_verified
        )
        if severity is None:
            continue
        event_id = f"disclosure:{disclosure.announcement_id}"
        result[event_id] = ActiveEventRisk(
            event_id=event_id,
            severity=severity,
            trusted_source=disclosure.official_verified,
            source=disclosure.source,
            title=disclosure.title,
            published_at=published_at,
        )

    for news_item in news:
        if symbol not in news_item.related_symbols:
            continue
        published_at = news_item.published_at or news_item.available_at
        if published_at > decision_at or published_at < window_start:
            continue
        severity = _severity_for_title(
            news_item.title, trusted=news_item.official_verified
        )
        if severity is None:
            continue
        event_id = f"news:{news_item.news_id}"
        result[event_id] = ActiveEventRisk(
            event_id=event_id,
            severity=severity,
            trusted_source=news_item.official_verified,
            source=news_item.source,
            title=news_item.title,
            published_at=published_at,
        )
    return tuple(
        sorted(result.values(), key=lambda item: (item.published_at or decision_at, item.event_id))
    )


def _severity_for_title(title: str, *, trusted: bool) -> EventSeverity | None:
    normalized = "".join(title.split())
    if trusted:
        if any(keyword in normalized for keyword in CRITICAL_OFFICIAL_KEYWORDS):
            return EventSeverity.CRITICAL
        if any(keyword in normalized for keyword in HIGH_OFFICIAL_KEYWORDS):
            return EventSeverity.HIGH
        return None
    if any(keyword in normalized for keyword in MEDIA_NEGATIVE_KEYWORDS):
        return EventSeverity.MEDIUM
    return None
