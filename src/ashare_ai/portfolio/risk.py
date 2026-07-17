from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PortfolioRiskState(StrEnum):
    NORMAL = "NORMAL"
    DERISK = "DERISK"
    OBSERVE_ONLY = "OBSERVE_ONLY"


class DrawdownConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    warning_threshold: Decimal = Field(gt=0, lt=1)
    fuse_threshold: Decimal = Field(gt=0, lt=1)
    derisk_gross_multiplier: Decimal = Field(gt=0, le=1)
    minimum_observation_sessions: int = Field(ge=0)
    recovery_threshold: Decimal = Field(ge=0, lt=1)

    @model_validator(mode="after")
    def validate_thresholds(self) -> DrawdownConfig:
        if self.warning_threshold >= self.fuse_threshold:
            raise ValueError("warning_threshold must be below fuse_threshold")
        if self.recovery_threshold > self.warning_threshold:
            raise ValueError("recovery_threshold must not exceed warning_threshold")
        return self


class DrawdownControlState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: PortfolioRiskState
    drawdown: Decimal = Field(ge=0, le=1)
    observation_sessions: int = Field(ge=0)


def evaluate_drawdown(
    *,
    nav: Decimal,
    high_watermark: Decimal,
    config: DrawdownConfig,
) -> tuple[PortfolioRiskState, Decimal]:
    if nav < 0 or high_watermark <= 0:
        raise ValueError("nav must be non-negative and high_watermark positive")
    drawdown = max(Decimal("0"), Decimal("1") - nav / high_watermark)
    if drawdown >= config.fuse_threshold:
        return PortfolioRiskState.OBSERVE_ONLY, drawdown
    if drawdown >= config.warning_threshold:
        return PortfolioRiskState.DERISK, drawdown
    return PortfolioRiskState.NORMAL, drawdown


def transition_drawdown_state(
    *,
    nav: Decimal,
    high_watermark: Decimal,
    config: DrawdownConfig,
    previous: DrawdownControlState | None = None,
    manual_recovery_confirmed: bool = False,
) -> DrawdownControlState:
    proposed_state, drawdown = evaluate_drawdown(
        nav=nav, high_watermark=high_watermark, config=config
    )
    if previous is not None and previous.state == PortfolioRiskState.OBSERVE_ONLY:
        observation_sessions = previous.observation_sessions + 1
        can_recover = (
            observation_sessions >= config.minimum_observation_sessions
            and drawdown <= config.recovery_threshold
            and manual_recovery_confirmed
        )
        return DrawdownControlState(
            state=PortfolioRiskState.NORMAL if can_recover else PortfolioRiskState.OBSERVE_ONLY,
            drawdown=drawdown,
            observation_sessions=0 if can_recover else observation_sessions,
        )
    if proposed_state == PortfolioRiskState.OBSERVE_ONLY:
        return DrawdownControlState(
            state=proposed_state,
            drawdown=drawdown,
            observation_sessions=1,
        )
    return DrawdownControlState(
        state=proposed_state,
        drawdown=drawdown,
        observation_sessions=0,
    )
