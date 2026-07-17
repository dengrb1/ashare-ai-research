from __future__ import annotations

from datetime import date
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from ashare_ai.core.contracts import (
    Candidate,
    PaperPortfolio,
    PortfolioPosition,
    RunStatus,
)
from ashare_ai.core.hashing import stable_hash
from ashare_ai.portfolio.risk import PortfolioRiskState


class PortfolioFailureCode(StrEnum):
    FUSED = "FUSED"
    INSUFFICIENT_CANDIDATES = "INSUFFICIENT_CANDIDATES"
    CAPACITY_INFEASIBLE = "CAPACITY_INFEASIBLE"
    INDUSTRY_INFEASIBLE = "INDUSTRY_INFEASIBLE"
    MISSING_STYLE_DATA = "MISSING_STYLE_DATA"
    STYLE_INFEASIBLE = "STYLE_INFEASIBLE"
    TURNOVER_INFEASIBLE = "TURNOVER_INFEASIBLE"
    INVALID_REFERENCE_PRICE = "INVALID_REFERENCE_PRICE"
    MIXED_TRADING_DATE = "MIXED_TRADING_DATE"
    MIXED_DECISION_AT = "MIXED_DECISION_AT"
    FUTURE_REFERENCE_PRICE = "FUTURE_REFERENCE_PRICE"
    ZERO_TARGET_SHARES = "ZERO_TARGET_SHARES"


class CandidateQuote(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate: Candidate
    reference_price: Decimal = Field(gt=0)
    available_at: AwareDatetime
    snapshot_hash: str = Field(min_length=64, max_length=64)
    lot_size: int = Field(gt=0)
    price_basis: Literal["RAW"] = "RAW"


class PortfolioConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_count: int = Field(gt=0)
    maximum_single_weight: Decimal = Field(gt=0, le=1)
    maximum_industry_weight: Decimal = Field(gt=0, le=1)
    maximum_turnover: Decimal = Field(ge=0, le=2)
    style_exposure_limits: dict[str, Decimal]
    base_cash_weight: Decimal = Field(ge=0, lt=1)
    minimum_prediction_percentile: Decimal = Field(ge=0, le=1)
    constraint_version: str
    enforce_turnover_on_initial: bool
    allocation_tolerance: Decimal = Field(gt=0)
    maximum_allocation_iterations: int = Field(gt=0)


class PortfolioBuildFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: PortfolioFailureCode
    message: str
    details: dict[str, object] = Field(default_factory=dict)


class PortfolioBuildResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    success: bool
    risk_state: PortfolioRiskState
    portfolio: PaperPortfolio | None = None
    failure: PortfolioBuildFailure | None = None


class PortfolioBuilder:
    def __init__(self, config: PortfolioConfig) -> None:
        self.config = config

    def build(
        self,
        *,
        quotes: list[CandidateQuote] | tuple[CandidateQuote, ...],
        nav: Decimal,
        effective_trading_date: date,
        current_weights: dict[str, float],
        risk_state: PortfolioRiskState,
        derisk_gross_multiplier: Decimal,
    ) -> PortfolioBuildResult:
        trading_dates = {quote.candidate.trading_date for quote in quotes}
        if len(trading_dates) > 1:
            return self._failure(
                risk_state,
                PortfolioFailureCode.MIXED_TRADING_DATE,
                "candidate quotes must share one trading_date",
                trading_dates=sorted(value.isoformat() for value in trading_dates),
            )
        decision_times = {quote.candidate.decision_at for quote in quotes}
        if len(decision_times) > 1:
            return self._failure(
                risk_state,
                PortfolioFailureCode.MIXED_DECISION_AT,
                "candidate quotes must share one decision_at",
                decision_at=sorted(value.isoformat() for value in decision_times),
            )
        future_quotes = sorted(
            quote.candidate.symbol
            for quote in quotes
            if quote.available_at > quote.candidate.decision_at
        )
        if future_quotes:
            return self._failure(
                risk_state,
                PortfolioFailureCode.FUTURE_REFERENCE_PRICE,
                "reference prices unavailable at portfolio decision time",
                symbols=future_quotes,
            )
        if risk_state == PortfolioRiskState.OBSERVE_ONLY:
            return self._failure(
                risk_state,
                PortfolioFailureCode.FUSED,
                "drawdown fuse is active; no new paper portfolio may be generated",
            )

        eligible = [
            quote
            for quote in quotes
            if Decimal(str(quote.candidate.prediction_percentile))
            >= self.config.minimum_prediction_percentile
            and quote.candidate.event_risk_multiplier > 0
        ]
        ranked = sorted(
            eligible,
            key=lambda quote: (
                -quote.candidate.total_score,
                -quote.candidate.prediction_percentile,
                quote.candidate.symbol,
            ),
        )
        selected = ranked[: self.config.target_count]
        if len(selected) < self.config.target_count:
            return self._failure(
                risk_state,
                PortfolioFailureCode.INSUFFICIENT_CANDIDATES,
                "not enough eligible candidates after percentile and event-risk filters",
                eligible_count=len(eligible),
                target_count=self.config.target_count,
            )

        investable_weight = Decimal("1") - self.config.base_cash_weight
        if risk_state == PortfolioRiskState.DERISK:
            investable_weight *= derisk_gross_multiplier

        if Decimal(len(selected)) * self.config.maximum_single_weight < investable_weight:
            return self._failure(
                risk_state,
                PortfolioFailureCode.CAPACITY_INFEASIBLE,
                "single-name caps cannot hold the requested investable weight",
            )
        industry_count = len({quote.candidate.industry_code for quote in selected})
        if Decimal(industry_count) * self.config.maximum_industry_weight < investable_weight:
            return self._failure(
                risk_state,
                PortfolioFailureCode.INDUSTRY_INFEASIBLE,
                "industry caps cannot hold the requested investable weight",
            )

        allocation = self._allocate(selected, investable_weight)
        if allocation is None:
            return self._failure(
                risk_state,
                PortfolioFailureCode.INDUSTRY_INFEASIBLE,
                "equal-risk allocation is infeasible under name and industry caps",
            )

        style_failure = self._validate_style_exposures(selected, allocation, risk_state)
        if style_failure is not None:
            return style_failure

        target_weights = {
            quote.candidate.symbol: float(allocation[quote.candidate.symbol]) for quote in selected
        }
        turnover = self._turnover(target_weights, current_weights)
        if (current_weights or self.config.enforce_turnover_on_initial) and Decimal(
            str(turnover)
        ) > self.config.maximum_turnover:
            return self._failure(
                risk_state,
                PortfolioFailureCode.TURNOVER_INFEASIBLE,
                "target exceeds the configured one-way turnover limit",
                expected_turnover=turnover,
                maximum_turnover=float(self.config.maximum_turnover),
            )

        positions: list[PortfolioPosition] = []
        for quote in selected:
            weight = allocation[quote.candidate.symbol]
            shares_decimal = nav * weight / quote.reference_price / quote.lot_size
            lots = int(shares_decimal.to_integral_value(rounding=ROUND_FLOOR))
            target_shares = lots * quote.lot_size
            if target_shares <= 0:
                return self._failure(
                    risk_state,
                    PortfolioFailureCode.ZERO_TARGET_SHARES,
                    "a selected target rounds to zero shares",
                    symbol=quote.candidate.symbol,
                )
            positions.append(
                PortfolioPosition(
                    symbol=quote.candidate.symbol,
                    industry_code=quote.candidate.industry_code,
                    weight=float(weight),
                    target_shares=target_shares,
                    reference_price=quote.reference_price,
                )
            )

        positions.sort(key=lambda position: position.symbol)
        cash_weight = Decimal("1") - sum(
            (Decimal(str(position.weight)) for position in positions),
            Decimal("0"),
        )
        decision_at = selected[0].candidate.decision_at
        trading_date = selected[0].candidate.trading_date
        input_payload = {
            "quotes": [
                quote.model_dump(mode="json")
                for quote in sorted(quotes, key=lambda item: item.candidate.symbol)
            ],
            "nav": nav,
            "current_weights": dict(sorted(current_weights.items())),
            "risk_state": risk_state,
            "config": self.config,
            "effective_trading_date": effective_trading_date,
        }
        input_hash = stable_hash(input_payload)
        portfolio = PaperPortfolio(
            portfolio_id=uuid5(NAMESPACE_URL, input_hash),
            trading_date=trading_date,
            decision_at=decision_at,
            effective_trading_date=effective_trading_date,
            status=RunStatus.SUCCEEDED,
            positions=tuple(positions),
            expected_turnover=turnover,
            cash_weight=float(cash_weight),
            constraint_version=self.config.constraint_version,
            input_hash=input_hash,
        )
        return PortfolioBuildResult(
            success=True,
            risk_state=risk_state,
            portfolio=portfolio,
        )

    def _validate_style_exposures(
        self,
        selected: list[CandidateQuote],
        allocation: dict[str, Decimal],
        risk_state: PortfolioRiskState,
    ) -> PortfolioBuildResult | None:
        for factor, limit in sorted(self.config.style_exposure_limits.items()):
            if limit < 0:
                raise ValueError(f"style exposure limit must be non-negative: {factor}")
            missing = [
                quote.candidate.symbol
                for quote in selected
                if factor not in quote.candidate.style_exposures
            ]
            if missing:
                return self._failure(
                    risk_state,
                    PortfolioFailureCode.MISSING_STYLE_DATA,
                    "selected candidates are missing required style exposures",
                    factor=factor,
                    symbols=missing,
                )
            exposure = sum(
                (
                    allocation[quote.candidate.symbol]
                    * Decimal(str(quote.candidate.style_exposures[factor]))
                    for quote in selected
                ),
                Decimal("0"),
            )
            if abs(exposure) > limit:
                return self._failure(
                    risk_state,
                    PortfolioFailureCode.STYLE_INFEASIBLE,
                    "weighted style exposure exceeds the configured limit",
                    factor=factor,
                    exposure=float(exposure),
                    limit=float(limit),
                )
        return None

    def _allocate(
        self,
        selected: list[CandidateQuote],
        total_weight: Decimal,
    ) -> dict[str, Decimal] | None:
        weights = {quote.candidate.symbol: Decimal("0") for quote in selected}
        industry_weights: dict[str, Decimal] = {}
        risk_scores = {
            quote.candidate.symbol: Decimal(str(quote.candidate.event_risk_multiplier))
            / Decimal(str(quote.candidate.volatility))
            for quote in selected
        }
        quote_by_symbol = {quote.candidate.symbol: quote for quote in selected}

        for _ in range(self.config.maximum_allocation_iterations):
            allocated = sum(weights.values(), Decimal("0"))
            remaining = total_weight - allocated
            if remaining <= self.config.allocation_tolerance:
                break
            active = [
                symbol
                for symbol in weights
                if weights[symbol] + self.config.allocation_tolerance
                < self.config.maximum_single_weight
                and industry_weights.get(
                    quote_by_symbol[symbol].candidate.industry_code,
                    Decimal("0"),
                )
                + self.config.allocation_tolerance
                < self.config.maximum_industry_weight
            ]
            if not active:
                return None
            score_total = sum((risk_scores[symbol] for symbol in active), Decimal("0"))
            if score_total <= 0:
                return None
            proposal = {symbol: remaining * risk_scores[symbol] / score_total for symbol in active}

            by_industry: dict[str, list[str]] = {}
            for symbol in active:
                industry = quote_by_symbol[symbol].candidate.industry_code
                by_industry.setdefault(industry, []).append(symbol)
            for industry, symbols in by_industry.items():
                proposed = sum((proposal[symbol] for symbol in symbols), Decimal("0"))
                room = self.config.maximum_industry_weight - industry_weights.get(
                    industry, Decimal("0")
                )
                if proposed > room and proposed > 0:
                    scale = room / proposed
                    for symbol in symbols:
                        proposal[symbol] *= scale

            progress = Decimal("0")
            for symbol in active:
                industry = quote_by_symbol[symbol].candidate.industry_code
                name_room = self.config.maximum_single_weight - weights[symbol]
                industry_room = self.config.maximum_industry_weight - industry_weights.get(
                    industry, Decimal("0")
                )
                increment = min(proposal[symbol], name_room, industry_room)
                if increment <= 0:
                    continue
                weights[symbol] += increment
                industry_weights[industry] = (
                    industry_weights.get(industry, Decimal("0")) + increment
                )
                progress += increment
            if progress <= self.config.allocation_tolerance:
                return None

        shortfall = total_weight - sum(weights.values(), Decimal("0"))
        if shortfall > self.config.allocation_tolerance:
            return None
        if shortfall > 0:
            for symbol in sorted(weights):
                industry = quote_by_symbol[symbol].candidate.industry_code
                room = min(
                    self.config.maximum_single_weight - weights[symbol],
                    self.config.maximum_industry_weight
                    - industry_weights.get(industry, Decimal("0")),
                )
                increment = min(room, shortfall)
                if increment > 0:
                    weights[symbol] += increment
                    industry_weights[industry] = (
                        industry_weights.get(industry, Decimal("0")) + increment
                    )
                    shortfall -= increment
                if shortfall <= 0:
                    break
        return weights if shortfall <= self.config.allocation_tolerance else None

    @staticmethod
    def _turnover(target: dict[str, float], current: dict[str, float]) -> float:
        symbols = set(target) | set(current)
        return 0.5 * sum(
            abs(target.get(symbol, 0.0) - current.get(symbol, 0.0)) for symbol in symbols
        )

    @staticmethod
    def _failure(
        risk_state: PortfolioRiskState,
        code: PortfolioFailureCode,
        message: str,
        **details: object,
    ) -> PortfolioBuildResult:
        return PortfolioBuildResult(
            success=False,
            risk_state=risk_state,
            failure=PortfolioBuildFailure(code=code, message=message, details=details),
        )
