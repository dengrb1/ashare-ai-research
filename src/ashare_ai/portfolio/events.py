from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


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
