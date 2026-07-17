from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ashare_ai.storage.models import TradingRuleRow


class TradingRuleError(RuntimeError):
    """Base error for a fail-closed trading-rule decision."""


class RuleNotFoundError(TradingRuleError):
    pass


class RuleConflictError(TradingRuleError):
    pass


class TradingRuleStorageRow(Protocol):
    rule_id: str
    rule_type: str
    rule_version: str
    priority: int
    exchange: str | None
    market: str | None
    board: str | None
    security_type: str | None
    risk_status: str | None
    is_st: bool | None
    symbol: str | None
    special_phase: str | None
    listing_session_from: int | None
    listing_session_to: int | None
    min_listing_days: int | None
    max_listing_days: int | None
    effective_from: date
    effective_to: date | None
    published_at: datetime | None
    enabled: bool
    price_limit_ratio: Decimal | None
    no_price_limit: bool
    lot_size: int
    t_plus_one: bool
    stamp_tax_rate: Decimal
    commission_rate: Decimal
    minimum_commission: Decimal
    transfer_fee_rate: Decimal
    raw_payload_sha256: str | None
    ingested_at: datetime | None
    details: dict[str, Any]


def _legacy_optional_text(details: Mapping[str, Any], field: str) -> str | None:
    value = details.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"legacy trading-rule detail {field!r} must be a string")
    return value


def _aware_or_none(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


class TradingRule(BaseModel):
    """Runtime representation of one effective-dated database trading rule."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_id: str
    rule_type: str = "COMPOSITE"
    rule_version: str
    priority: int
    exchange: str | None = None
    market: str | None = None
    board: str | None = None
    security_type: str | None = None
    risk_status: str | None = None
    is_st: bool | None = None
    symbol: str | None = None
    special_phase: str | None = None
    listing_session_from: int | None = Field(default=None, ge=0)
    listing_session_to: int | None = Field(default=None, ge=0)
    min_listing_days: int | None = Field(default=None, ge=0)
    max_listing_days: int | None = Field(default=None, ge=0)
    effective_from: date
    effective_to: date | None = None
    published_at: AwareDatetime | None = None
    available_at: AwareDatetime | None = None
    enabled: bool = True
    source_snapshot_hash: str | None = None
    price_limit_ratio: Decimal | None = Field(default=None, ge=0)
    no_price_limit: bool = False
    lot_size: int = Field(gt=0)
    t_plus_one: bool = True
    stamp_tax_rate: Decimal = Field(ge=0)
    commission_rate: Decimal = Field(ge=0)
    minimum_commission: Decimal = Field(ge=0)
    transfer_fee_rate: Decimal = Field(ge=0)
    details: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_ranges(self) -> TradingRule:
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be after effective_from")
        if (
            self.min_listing_days is not None
            and self.max_listing_days is not None
            and self.max_listing_days < self.min_listing_days
        ):
            raise ValueError("max_listing_days must be >= min_listing_days")
        if (
            self.listing_session_from is not None
            and self.listing_session_to is not None
            and self.listing_session_to < self.listing_session_from
        ):
            raise ValueError("listing_session_to must be >= listing_session_from")
        if not self.no_price_limit and self.price_limit_ratio is None:
            raise ValueError("limited trading rule requires price_limit_ratio")
        return self

    @classmethod
    def from_storage_row(cls, row: TradingRuleStorageRow) -> TradingRule:
        details = dict(row.details or {})
        return cls(
            rule_id=str(row.rule_id),
            rule_type=row.rule_type,
            rule_version=str(row.rule_version),
            priority=int(row.priority),
            exchange=row.exchange,
            market=row.market,
            board=row.board,
            security_type=row.security_type,
            risk_status=row.risk_status,
            is_st=row.is_st,
            symbol=(
                row.symbol if row.symbol is not None else _legacy_optional_text(details, "symbol")
            ),
            special_phase=(
                row.special_phase
                if row.special_phase is not None
                else _legacy_optional_text(details, "special_phase")
            ),
            listing_session_from=row.listing_session_from,
            listing_session_to=row.listing_session_to,
            min_listing_days=row.min_listing_days,
            max_listing_days=row.max_listing_days,
            effective_from=row.effective_from,
            effective_to=row.effective_to,
            published_at=_aware_or_none(row.published_at),
            available_at=_aware_or_none(row.ingested_at),
            enabled=bool(row.enabled),
            source_snapshot_hash=row.raw_payload_sha256,
            price_limit_ratio=row.price_limit_ratio,
            no_price_limit=bool(row.no_price_limit),
            lot_size=int(row.lot_size),
            t_plus_one=bool(row.t_plus_one),
            stamp_tax_rate=row.stamp_tax_rate,
            commission_rate=row.commission_rate,
            minimum_commission=row.minimum_commission,
            transfer_fee_rate=row.transfer_fee_rate,
            details=details,
        )


class RuleContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    trading_date: date
    decision_at: AwareDatetime
    exchange: str
    market: str
    board: str
    security_type: str
    risk_status: str
    is_st: bool
    listing_days: int = Field(ge=0)
    listing_session: int = Field(ge=0)
    special_phase: str = "NORMAL"


class TradingRuleBook:
    """Deterministic rule matcher; ambiguity and absence are fatal."""

    def __init__(self, rules: list[TradingRule] | tuple[TradingRule, ...]) -> None:
        self._rules = tuple(rules)

    def resolve(self, context: RuleContext) -> TradingRule:
        matches = [rule for rule in self._rules if self._matches(rule, context)]
        if not matches:
            raise RuleNotFoundError(
                "no safe trading rule for "
                f"{context.symbol} {context.trading_date} {context.board} "
                f"st={context.is_st} phase={context.special_phase}"
            )

        ranked = sorted(matches, key=lambda rule: self._rank(rule, context), reverse=True)
        top_rank = self._rank(ranked[0], context)
        conflicts = [rule for rule in ranked if self._rank(rule, context) == top_rank]
        if len(conflicts) > 1:
            ids = ", ".join(sorted(rule.rule_id for rule in conflicts))
            raise RuleConflictError(f"ambiguous trading rules: {ids}")
        return ranked[0]

    @staticmethod
    def _matches(rule: TradingRule, context: RuleContext) -> bool:
        if not rule.enabled:
            return False
        if rule.published_at is not None and rule.published_at > context.decision_at:
            return False
        if rule.available_at is not None and rule.available_at > context.decision_at:
            return False
        if context.trading_date < rule.effective_from:
            return False
        if rule.effective_to is not None and context.trading_date >= rule.effective_to:
            return False
        if rule.symbol is not None and rule.symbol != context.symbol:
            return False
        if rule.exchange is not None and rule.exchange != context.exchange:
            return False
        if rule.market is not None and rule.market != context.market:
            return False
        if rule.board is not None and rule.board != context.board:
            return False
        if rule.security_type is not None and rule.security_type != context.security_type:
            return False
        if rule.risk_status is not None and rule.risk_status != context.risk_status:
            return False
        if rule.is_st is not None and rule.is_st != context.is_st:
            return False
        if (
            context.is_st
            and rule.is_st is None
            and not bool(rule.details.get("applies_all_st", False))
        ):
            return False
        if rule.special_phase is not None and rule.special_phase != context.special_phase:
            return False
        if (
            rule.listing_session_from is not None
            and context.listing_session < rule.listing_session_from
        ):
            return False
        if (
            rule.listing_session_to is not None
            and context.listing_session > rule.listing_session_to
        ):
            return False
        if rule.min_listing_days is not None and context.listing_days < rule.min_listing_days:
            return False
        return not (
            rule.max_listing_days is not None and context.listing_days > rule.max_listing_days
        )

    @staticmethod
    def _rank(rule: TradingRule, context: RuleContext) -> tuple[int, ...]:
        del context
        bounded = any(
            value is not None
            for value in (
                rule.listing_session_from,
                rule.listing_session_to,
                rule.min_listing_days,
                rule.max_listing_days,
            )
        )
        lower = rule.listing_session_from
        if lower is None:
            lower = rule.min_listing_days if rule.min_listing_days is not None else 0
        upper = rule.listing_session_to
        if upper is None:
            upper = rule.max_listing_days if rule.max_listing_days is not None else 10**9
        width = upper - lower
        return (
            int(rule.symbol is not None),
            int(rule.special_phase is not None),
            int(bounded),
            int(rule.exchange is not None),
            int(rule.board is not None),
            int(rule.market is not None),
            int(rule.security_type is not None),
            int(rule.risk_status is not None),
            int(rule.is_st is not None),
            -width,
            rule.priority,
        )


class TradingRuleRepository:
    """Point-in-time loader over the storage trading-rule table."""

    def load_visible(self, session: Session, context: RuleContext) -> tuple[TradingRule, ...]:
        statement = (
            select(TradingRuleRow)
            .where(
                TradingRuleRow.enabled.is_(True),
                TradingRuleRow.effective_from <= context.trading_date,
                or_(
                    TradingRuleRow.effective_to.is_(None),
                    TradingRuleRow.effective_to > context.trading_date,
                ),
                or_(
                    TradingRuleRow.published_at.is_(None),
                    TradingRuleRow.published_at <= context.decision_at,
                ),
                or_(
                    TradingRuleRow.ingested_at.is_(None),
                    TradingRuleRow.ingested_at <= context.decision_at,
                ),
            )
            .order_by(TradingRuleRow.rule_id)
        )
        return tuple(TradingRule.from_storage_row(row) for row in session.scalars(statement))

    def resolve(self, session: Session, context: RuleContext) -> TradingRule:
        return TradingRuleBook(self.load_visible(session, context)).resolve(context)


class PriceLimitPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    require_official_limits: bool
    default_price_tick: Decimal = Field(gt=0)


class PriceBand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lower: Decimal | None
    upper: Decimal | None
    source: str


def _round_to_tick(value: Decimal, tick: Decimal) -> Decimal:
    units = (value / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return units * tick


def resolve_price_band(
    rule: TradingRule,
    *,
    prev_close: Decimal | None,
    official_limit_up: Decimal | None,
    official_limit_down: Decimal | None,
    policy: PriceLimitPolicy,
) -> PriceBand:
    if rule.no_price_limit:
        return PriceBand(lower=None, upper=None, source="NO_LIMIT")

    if (official_limit_up is None) != (official_limit_down is None):
        raise RuleNotFoundError("official price band must provide both upper and lower values")
    if official_limit_up is not None and official_limit_down is not None:
        if official_limit_down >= official_limit_up:
            raise RuleConflictError("official lower limit must be below upper limit")
        return PriceBand(
            lower=official_limit_down,
            upper=official_limit_up,
            source="OFFICIAL",
        )

    if policy.require_official_limits:
        raise RuleNotFoundError("official price limits are required")
    if prev_close is None or rule.price_limit_ratio is None:
        raise RuleNotFoundError("cannot derive price limits without previous close and ratio")

    tick = Decimal(str(rule.details.get("price_tick", policy.default_price_tick)))
    if tick <= 0:
        raise RuleConflictError("price tick must be positive")
    upper = _round_to_tick(prev_close * (Decimal("1") + rule.price_limit_ratio), tick)
    lower = _round_to_tick(prev_close * (Decimal("1") - rule.price_limit_ratio), tick)
    return PriceBand(lower=lower, upper=upper, source="DERIVED")
