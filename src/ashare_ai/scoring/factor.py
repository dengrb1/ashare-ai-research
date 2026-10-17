"""Versioned, parameterizable deterministic factor scoring.

The composite formula in :mod:`ashare_ai.scoring.formula` is the frozen
production formula.  This module exposes the same arithmetic as a
:class:`FactorGenome`: an explicit, strictly validated parameter set that the
low-frequency evolution job may propose new values for.

Two invariants hold for every genome:

* the score is a pure function of its inputs — no clock, no RNG, no I/O;
* the genome is fully described by its parameters, so ``parameter_sha256``
  identifies the exact formula that produced a score.

Nothing here places orders or reads live quotes.  A genome only ever changes
how frozen, point-in-time evidence is weighted.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from ashare_ai.core.contracts import FrozenModel
from ashare_ai.core.hashing import stable_hash

FACTOR_FORMULA_VERSION = "factor-v1.0.0"
SHADOW_FACTOR_FORMULA_VERSION = "factor-v2.0.0"
FACTOR_GENOME_SCHEMA_VERSION = "factor-genome-v1"

# The genome may only move weights and thresholds inside these bounds.  They
# are deliberately narrower than the Pydantic field bounds so a proposal can
# never collapse a component to zero or invert the decision bands.
MIN_COMPONENT_WEIGHT = 0.05
MAX_COMPONENT_WEIGHT = 0.60
MIN_THRESHOLD_GAP = 5.0

PositionLevel = Literal[0, 10, 20, 30, 50, 70, 100]
_POSITION_LEVELS: tuple[int, ...] = (0, 10, 20, 30, 50, 70, 100)


class FactorGenome(FrozenModel):
    """Strictly validated parameters of the deterministic factor formula."""

    schema_version: str = FACTOR_GENOME_SCHEMA_VERSION
    formula_version: str = FACTOR_FORMULA_VERSION

    fundamental_weight: float = Field(default=0.35, ge=MIN_COMPONENT_WEIGHT, le=MAX_COMPONENT_WEIGHT)
    technical_weight: float = Field(default=0.35, ge=MIN_COMPONENT_WEIGHT, le=MAX_COMPONENT_WEIGHT)
    sentiment_weight: float = Field(default=0.20, ge=MIN_COMPONENT_WEIGHT, le=MAX_COMPONENT_WEIGHT)
    quality_weight: float = Field(default=0.10, ge=MIN_COMPONENT_WEIGHT, le=MAX_COMPONENT_WEIGHT)
    # v2 shadow-only components.  They remain zero in the v1 genome so the
    # existing decision and research hashes are unchanged.
    momentum_weight: float = Field(default=0.0, ge=0.0, le=0.30)
    volatility_weight: float = Field(default=0.0, ge=0.0, le=0.30)
    liquidity_weight: float = Field(default=0.0, ge=0.0, le=0.30)

    # Score bands.  ``buy_threshold`` is the lower bound of BUY and
    # ``sell_threshold`` the upper bound of SELL; HOLD sits between them.
    buy_threshold: float = Field(default=70.0, ge=0.0, le=100.0)
    sell_threshold: float = Field(default=50.0, ge=0.0, le=100.0)
    risk_low_threshold: float = Field(default=70.0, ge=0.0, le=100.0)
    risk_high_threshold: float = Field(default=50.0, ge=0.0, le=100.0)

    max_position: PositionLevel = 70
    risk_budget: float = Field(default=0.20, gt=0.0, le=1.0)
    mutation_seed: int = Field(default=0, ge=0)

    @property
    def min_score_for_low_risk(self) -> float:
        """Semantic alias for the lower score bound of the LOW risk band."""

        return self.risk_low_threshold

    @property
    def max_score_for_high_risk(self) -> float:
        """Semantic alias for the upper score bound of the HIGH risk band."""

        return self.risk_high_threshold

    @model_validator(mode="after")
    def validate_bands(self) -> FactorGenome:
        if self.schema_version != FACTOR_GENOME_SCHEMA_VERSION:
            raise ValueError(f"unsupported factor genome schema: {self.schema_version}")
        if self.formula_version not in {FACTOR_FORMULA_VERSION, SHADOW_FACTOR_FORMULA_VERSION}:
            raise ValueError(f"unsupported factor formula: {self.formula_version}")
        if self.formula_version == FACTOR_FORMULA_VERSION and any(
            value != 0.0
            for value in (self.momentum_weight, self.volatility_weight, self.liquidity_weight)
        ):
            raise ValueError("v1 factor genomes must keep shadow weights at zero")
        total = (
            self.fundamental_weight
            + self.technical_weight
            + self.sentiment_weight
            + self.quality_weight
            + self.momentum_weight
            + self.volatility_weight
            + self.liquidity_weight
        )
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"factor weights must sum to 1, got {total}")
        if self.buy_threshold - self.sell_threshold < MIN_THRESHOLD_GAP:
            raise ValueError(
                f"buy_threshold must exceed sell_threshold by at least {MIN_THRESHOLD_GAP}"
            )
        if self.risk_low_threshold - self.risk_high_threshold < MIN_THRESHOLD_GAP:
            raise ValueError(
                "risk_low_threshold must exceed risk_high_threshold by at least "
                f"{MIN_THRESHOLD_GAP}"
            )
        return self

    @property
    def parameter_sha256(self) -> str:
        """Stable identity of this exact parameter set."""

        payload = self.model_dump(mode="json")
        # The v1 production contract predates the shadow-only fields. Keep its
        # parameter identity byte-for-byte stable while v2 gets a full hash.
        if self.formula_version == FACTOR_FORMULA_VERSION:
            payload = {
                key: value
                for key, value in payload.items()
                if key not in {"momentum_weight", "volatility_weight", "liquidity_weight"}
            }
        return stable_hash(payload)

    def with_parameters(self, **updates: object) -> FactorGenome:
        """Return a re-validated copy; invalid proposals raise ``ValueError``."""

        return FactorGenome.model_validate({**self.model_dump(mode="python"), **updates})


DEFAULT_FACTOR_GENOME = FactorGenome()

# Shadow formula used for parallel display and walk-forward evaluation.  The
# old four components retain 85% of the score and each new PIT component gets
# 5%.  It is never selected as the active genome automatically.
SHADOW_FACTOR_GENOME = FactorGenome(
    formula_version=SHADOW_FACTOR_FORMULA_VERSION,
    fundamental_weight=0.2975,
    technical_weight=0.2975,
    sentiment_weight=0.17,
    quality_weight=0.085,
    momentum_weight=0.05,
    volatility_weight=0.05,
    liquidity_weight=0.05,
)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def calculate_factor_score(
    *,
    fundamental_score: float,
    technical_score: float,
    sentiment_score: float,
    quality_confidence_score: float,
    genome: FactorGenome = DEFAULT_FACTOR_GENOME,
    dividend_bonus: float = 0.0,
    event_risk_multiplier: float = 1.0,
    market_score_adjustment: float = 0.0,
    market_risk_multiplier: float = 1.0,
    momentum_score: float = 50.0,
    volatility_score: float = 50.0,
    liquidity_score: float = 50.0,
) -> float:
    """Deterministic weighted score for one symbol on one trading date.

    The arithmetic mirrors ``composite-35-35-20-10-dividend-news-market-v3``
    with the genome's weights substituted, so the default genome reproduces the
    frozen production formula exactly.
    """

    components = {
        "fundamental": fundamental_score,
        "technical": technical_score,
        "sentiment": sentiment_score,
        "quality": quality_confidence_score,
        "momentum": momentum_score,
        "volatility": volatility_score,
        "liquidity": liquidity_score,
    }
    if any(score < 0 or score > 100 for score in components.values()):
        raise ValueError("all component scores must be within [0, 100]")
    if not 0 <= dividend_bonus <= 10:
        raise ValueError("dividend_bonus must be within [0, 10]")
    if not 0 <= event_risk_multiplier <= 1:
        raise ValueError("event_risk_multiplier must be within [0, 1]")
    if not -10 <= market_score_adjustment <= 10:
        raise ValueError("market_score_adjustment must be within [-10, 10]")
    if not 0.8 <= market_risk_multiplier <= 1:
        raise ValueError("market_risk_multiplier must be within [0.8, 1]")

    adjusted_fundamental = min(100.0, fundamental_score + dividend_bonus)
    base_total = (
        adjusted_fundamental * genome.fundamental_weight
        + technical_score * genome.technical_weight
        + sentiment_score * genome.sentiment_weight
        + quality_confidence_score * genome.quality_weight
        + momentum_score * genome.momentum_weight
        + volatility_score * genome.volatility_weight
        + liquidity_score * genome.liquidity_weight
    )
    return round(
        _clamp(base_total + market_score_adjustment, 0.0, 100.0)
        * event_risk_multiplier
        * market_risk_multiplier,
        6,
    )


def factor_tilt(score: float, genome: FactorGenome) -> float:
    """Signed position of ``score`` inside the HOLD band, clamped to [-1, 1]."""

    span = max(1e-6, genome.buy_threshold - genome.sell_threshold)
    midpoint = (genome.buy_threshold + genome.sell_threshold) / 2.0
    return _clamp((score - midpoint) / span, -1.0, 1.0)


def factor_action(score: float, genome: FactorGenome) -> Literal["BUY", "HOLD", "SELL"]:
    if score >= genome.buy_threshold:
        return "BUY"
    if score < genome.sell_threshold:
        return "SELL"
    return "HOLD"


def factor_risk(score: float, genome: FactorGenome) -> Literal["LOW", "MEDIUM", "HIGH"]:
    if score >= genome.risk_low_threshold:
        return "LOW"
    if score < genome.risk_high_threshold:
        return "HIGH"
    return "MEDIUM"


def factor_position(score: float, genome: FactorGenome) -> int:
    """Position tier for ``score``, never above the genome's ``max_position``."""

    if score >= genome.buy_threshold + 10:
        tier = 70
    elif score >= genome.buy_threshold:
        tier = 50
    elif score >= genome.sell_threshold:
        tier = 20
    elif score >= genome.sell_threshold - 10:
        tier = 10
    else:
        tier = 0
    return min(tier, genome.max_position)


def factor_position_level(score: float, genome: FactorGenome) -> PositionLevel:
    """``factor_position`` narrowed to the protocol's position enum."""

    tier = factor_position(score, genome)
    if tier not in _POSITION_LEVELS:  # pragma: no cover - defensive
        raise ValueError(f"unsupported position tier: {tier}")
    return tier  # type: ignore[return-value]
