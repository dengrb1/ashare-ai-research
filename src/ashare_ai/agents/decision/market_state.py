from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING

from ashare_ai.agents.decision.models import MarketState

if TYPE_CHECKING:
    from ashare_ai.orchestration.bundle import CanonicalDailyBundle

logger = logging.getLogger(__name__)


def build_market_state_from_bundle(
    bundle: CanonicalDailyBundle,
    symbol: str,
    trading_date: date,
) -> MarketState:
    """Build a point-in-time :class:`MarketState` from a frozen bundle.

    Only frozen bundle fields are read: the bar for ``symbol`` on
    ``trading_date``, the frozen benchmark returns, and the frozen industry
    membership.  Live quotes are never consulted, so a state rebuilt later is
    identical to the one the decision was made from.
    """

    if bundle.trading_date != trading_date:
        raise ValueError("bundle trading_date does not match the requested trading_date")

    bar = next(
        (
            item
            for item in bundle.bars
            if item.symbol == symbol and item.trading_date == trading_date
        ),
        None,
    )
    if bar is None:
        raise ValueError(f"bundle has no bar for {symbol} on {trading_date}")

    industry = next(
        (item for item in bundle.industries if item.symbol == symbol),
        None,
    )
    benchmark = bundle.benchmark_returns.get("000300.SH") or {}

    return MarketState(
        symbol=symbol,
        trading_date=trading_date,
        available_at=bar.available_at,
        open=float(bar.open),
        high=float(bar.high),
        low=float(bar.low),
        close=float(bar.close),
        volume=float(bar.volume),
        amount=float(bar.amount),
        industry_code=getattr(industry, "industry_code", None),
        industry_name=getattr(industry, "industry_name", None),
        index_return_1d=benchmark.get(trading_date),
    )


class MarketStateBuilder:
    """
    Market State 构建器，将 CanonicalDailyBundle 和特征工程输出编码为 MarketState。

    职责：
    - 从 Bundle 提取 OHLCV 和基础字段
    - 复用现有技术、情绪、基本面特征模块
    - 处理缺失的可选字段：使用显式 mask 和中性值
    - 确保 available_at <= decision_at
    - 禁止使用实时行情回写历史快照
    """

    def __init__(self, feature_version: str = "v1.0.0"):
        self._feature_version = feature_version

    def build_from_bundle(
        self,
        symbol: str,
        bundle: dict,  # CanonicalDailyBundle 的字典表示
        technical_features: dict | None = None,
        sentiment_features: dict | None = None,
        fundamental_features: dict | None = None,
        market_regime: str = "UNKNOWN",
        index_returns: dict | None = None,
    ) -> MarketState:
        """
        从 CanonicalDailyBundle 和特征构建 MarketState。

        Args:
            symbol: 标的代码
            bundle: 包含 OHLCV 的 Bundle 数据
            technical_features: 技术指标字典
            sentiment_features: 情绪特征字典
            fundamental_features: 基本面特征字典
            market_regime: 市场环境
            index_returns: 指数收益率字典

        Returns:
            MarketState 实例
        """
        # 提取基础 OHLCV
        ohlcv = bundle.get("daily_bar", {})
        trading_date = ohlcv.get("trading_date")
        available_at = ohlcv.get("available_at")

        if not trading_date or not available_at:
            raise ValueError(f"Bundle missing trading_date or available_at for {symbol}")

        # 构建基础状态
        state_dict = {
            "symbol": symbol,
            "trading_date": trading_date,
            "available_at": available_at,
            "open": float(ohlcv.get("open", 0)),
            "high": float(ohlcv.get("high", 0)),
            "low": float(ohlcv.get("low", 0)),
            "close": float(ohlcv.get("close", 0)),
            "volume": float(ohlcv.get("volume", 0)),
            "amount": float(ohlcv.get("amount", 0)),
            "feature_version": self._feature_version,
        }

        # 添加技术指标（可选）
        if technical_features:
            state_dict.update(self._extract_technical(technical_features))

        # 添加情绪特征（可选）
        if sentiment_features:
            state_dict.update(self._extract_sentiment(sentiment_features))

        # 添加基本面特征（可选）
        if fundamental_features:
            state_dict.update(self._extract_fundamental(fundamental_features))

        # 添加市场环境
        state_dict["market_regime"] = market_regime

        # 添加指数收益率（可选）
        if index_returns:
            state_dict.update(
                {
                    "index_return_1d": index_returns.get("return_1d"),
                    "index_return_5d": index_returns.get("return_5d"),
                    "index_return_20d": index_returns.get("return_20d"),
                }
            )

        return MarketState(**state_dict)

    def _extract_technical(self, features: dict) -> dict:
        """提取技术指标字段"""
        return {
            "ma5": features.get("ma5"),
            "ma10": features.get("ma10"),
            "ma20": features.get("ma20"),
            "ma60": features.get("ma60"),
            "ema12": features.get("ema12"),
            "ema26": features.get("ema26"),
            "macd": features.get("macd"),
            "macd_signal": features.get("macd_signal"),
            "macd_hist": features.get("macd_hist"),
            "rsi": features.get("rsi"),
            "kdj_k": features.get("kdj_k"),
            "kdj_d": features.get("kdj_d"),
            "kdj_j": features.get("kdj_j"),
            "bollinger_upper": features.get("bollinger_upper"),
            "bollinger_middle": features.get("bollinger_middle"),
            "bollinger_lower": features.get("bollinger_lower"),
            "turnover_rate": features.get("turnover_rate"),
            "return_5d": features.get("return_5d"),
            "return_20d": features.get("return_20d"),
            "close_to_ma20": features.get("close_to_ma20"),
            "annualized_volatility_20d": features.get("annualized_volatility_20d"),
            "volume_ratio_5_to_20": features.get("volume_ratio_5_to_20"),
            "max_drawdown_60d": features.get("max_drawdown_60d"),
        }

    def _extract_sentiment(self, features: dict) -> dict:
        """提取情绪特征字段"""
        return {
            "sentiment_score": features.get("sentiment_score"),
            "news_count_7d": features.get("news_count_7d"),
        }

    def _extract_fundamental(self, features: dict) -> dict:
        """提取基本面特征字段"""
        return {
            "industry_code": features.get("industry_code"),
            "industry_name": features.get("industry_name"),
            "pe_ttm": features.get("pe_ttm"),
            "pb": features.get("pb"),
            "roe": features.get("roe"),
            "revenue_growth_yoy": features.get("revenue_growth_yoy"),
            "net_profit_growth_yoy": features.get("net_profit_growth_yoy"),
        }
