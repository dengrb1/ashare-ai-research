from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from ashare_ai.core.contracts import CanonicalSymbol, FrozenModel

DecisionMode = Literal["legacy", "jev"]
DecisionAction = Literal["BUY", "HOLD", "SELL"]
DecisionRisk = Literal["LOW", "MEDIUM", "HIGH"]
PositionLevel = Literal[0, 10, 20, 30, 50, 70, 100]


class DirectionProbabilities(FrozenModel):
    """三分类方向概率分布：UP/FLAT/DOWN"""

    UP: float = Field(ge=0, le=1)
    FLAT: float = Field(ge=0, le=1)
    DOWN: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def probabilities_sum_to_one(self) -> DirectionProbabilities:
        total = self.UP + self.FLAT + self.DOWN
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"direction probabilities must sum to 1, got {total}")
        return self


class BinaryProbabilities(FrozenModel):
    """二分类概率分布：YES/NO"""

    YES: float = Field(ge=0, le=1)
    NO: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def probabilities_sum_to_one(self) -> BinaryProbabilities:
        total = self.YES + self.NO
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"binary probabilities must sum to 1, got {total}")
        return self


class ActionProbabilities(FrozenModel):
    """动作概率分布：BUY/HOLD/SELL"""

    BUY: float = Field(ge=0, le=1)
    HOLD: float = Field(ge=0, le=1)
    SELL: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def probabilities_sum_to_one(self) -> ActionProbabilities:
        total = self.BUY + self.HOLD + self.SELL
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"action probabilities must sum to 1, got {total}")
        return self


class RiskProbabilities(FrozenModel):
    """风险等级概率分布：LOW/MEDIUM/HIGH"""

    LOW: float = Field(ge=0, le=1)
    MEDIUM: float = Field(ge=0, le=1)
    HIGH: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def probabilities_sum_to_one(self) -> RiskProbabilities:
        total = self.LOW + self.MEDIUM + self.HIGH
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"risk probabilities must sum to 1, got {total}")
        return self


class DecisionProbabilities(FrozenModel):
    """统一的决策概率输出，包含所有任务头"""

    direction_1d: DirectionProbabilities
    direction_5d: DirectionProbabilities
    up_over_3pct_5d: BinaryProbabilities
    action: ActionProbabilities
    risk: RiskProbabilities


class UnifiedDecision(FrozenModel):
    """统一决策协议输出"""

    symbol: CanonicalSymbol
    trading_date: date
    available_at: AwareDatetime
    decision_at: AwareDatetime
    probabilities: DecisionProbabilities
    action: DecisionAction
    risk: DecisionRisk
    position: PositionLevel
    mode: DecisionMode
    model_version: str
    input_manifest_hash: str = Field(min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_time_consistency(self) -> UnifiedDecision:
        if self.decision_at.date() != self.trading_date:
            raise ValueError("decision_at must fall on trading_date")
        if self.available_at > self.decision_at:
            raise ValueError("available_at must not be after decision_at")
        return self

    @field_validator("input_manifest_hash")
    @classmethod
    def validate_hash_format(cls, value: str) -> str:
        if len(value) != 64 or not all(c in "0123456789abcdef" for c in value):
            raise ValueError("input_manifest_hash must be 64-character hex SHA-256")
        return value


class MarketState(FrozenModel):
    """市场状态输入，包含所有可用特征"""

    symbol: CanonicalSymbol
    trading_date: date
    available_at: AwareDatetime

    # OHLCV 基础数据
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float = Field(ge=0)
    amount: float = Field(ge=0)
    turnover_rate: float | None = Field(default=None, ge=0, le=1)

    # 技术指标
    ma5: float | None = None
    ma10: float | None = None
    ma20: float | None = None
    ma60: float | None = None
    ema12: float | None = None
    ema26: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    macd_hist: float | None = None
    rsi: float | None = Field(default=None, ge=0, le=100)
    kdj_k: float | None = Field(default=None, ge=0, le=100)
    kdj_d: float | None = Field(default=None, ge=0, le=100)
    kdj_j: float | None = None
    bollinger_upper: float | None = Field(default=None, gt=0)
    bollinger_middle: float | None = Field(default=None, gt=0)
    bollinger_lower: float | None = Field(default=None, gt=0)

    # 市场环境
    market_regime: Literal["RISK_ON", "NEUTRAL", "RISK_OFF", "UNKNOWN"] = "UNKNOWN"
    index_return_1d: float | None = None
    index_return_5d: float | None = None
    index_return_20d: float | None = None

    # 行业和基本面
    industry_code: str | None = None
    industry_name: str | None = None
    pe_ttm: float | None = Field(default=None, gt=0)
    pb: float | None = Field(default=None, gt=0)
    roe: float | None = None
    revenue_growth_yoy: float | None = None
    net_profit_growth_yoy: float | None = None

    # 情绪和新闻（可选）
    sentiment_score: float | None = Field(default=None, ge=0, le=100)
    news_count_7d: int | None = Field(default=None, ge=0)

    # 特征版本
    feature_version: str = "v1.0.0"

    @model_validator(mode="after")
    def validate_ohlc(self) -> MarketState:
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high is below OHLC values")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low is above OHLC values")
        return self
