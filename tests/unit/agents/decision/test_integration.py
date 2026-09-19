"""
决策模式测试覆盖 - 集成测试。

测试决策模块与现有系统的集成点：
- CompositeScore 转换
- Bundle 集成
- CLI 命令
- API 端点
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from ashare_ai.agents.decision.legacy import (
    LegacyDecisionProvider,
    convert_composite_score_to_decision,
)
from ashare_ai.agents.decision.market_state import MarketState
from ashare_ai.core.contracts import CompositeScore, DataQualityInputs, MarketIndexSnapshot


@pytest.fixture
def sample_composite_score() -> CompositeScore:
    """示例 CompositeScore 用于集成测试"""
    return CompositeScore(
        symbol="600000.SH",
        trading_date=date(2026, 7, 17),
        decision_at=datetime(2026, 7, 17, 15, 30, tzinfo=timezone.utc),
        fundamental_score=75.0,
        technical_score=68.0,
        sentiment_score=72.0,
        quality_confidence_score=85.0,
        total_score=74.5,
        formula_version="composite-35-35-20-10-v1",
        agent_bundle_sha256="a" * 64,
        feature_snapshot_id="00000000-0000-0000-0000-000000000001",
        evidence_bundle_sha256="b" * 64,
        dividend_bonus=0.0,
        event_risk_multiplier=1.0,
    )


@pytest.fixture
def sample_market_state() -> MarketState:
    """示例 MarketState"""
    return MarketState(
        symbol="600000.SH",
        trading_date=date(2026, 7, 17),
        available_at=datetime(2026, 7, 17, 17, 0, tzinfo=timezone.utc),
        open=10.0,
        high=10.5,
        low=9.8,
        close=10.2,
        volume=1000000.0,
        amount=10000000.0,
    )


@pytest.mark.asyncio
async def test_legacy_provider_with_composite_score(
    sample_composite_score: CompositeScore,
    sample_market_state: MarketState,
) -> None:
    """测试 LegacyDecisionProvider 接受 CompositeScore"""
    provider = LegacyDecisionProvider(model_version="legacy-v1.0.0")

    decision = await provider.predict(sample_market_state, composite_score=sample_composite_score)

    assert decision.symbol == "600000.SH"
    assert decision.trading_date == date(2026, 7, 17)
    assert decision.mode == "legacy"
    assert decision.model_version == "legacy-v1.0.0"
    # 基于 total_score=74.5 应该得到 BUY
    assert decision.action == "BUY"
    assert decision.position >= 50


@pytest.mark.asyncio
async def test_legacy_provider_without_composite_score(sample_market_state: MarketState) -> None:
    """测试 LegacyDecisionProvider 的 fallback 规则"""
    provider = LegacyDecisionProvider(model_version="legacy-v1.0.0")

    decision = await provider.predict(sample_market_state)

    assert decision.symbol == "600000.SH"
    assert decision.mode == "legacy"
    # Fallback 使用 score=50，应该是 HOLD
    assert decision.action == "HOLD"


def test_convert_composite_score_to_decision(
    sample_composite_score: CompositeScore,
    sample_market_state: MarketState,
) -> None:
    """测试 CompositeScore 转换工具函数"""
    decision = convert_composite_score_to_decision(
        composite_score=sample_composite_score,
        market_state=sample_market_state,
        model_version="legacy-v1.0.0",
    )

    assert decision.symbol == sample_composite_score.symbol
    assert decision.trading_date == sample_composite_score.trading_date
    assert decision.decision_at == sample_composite_score.decision_at
    assert decision.mode == "legacy"
    assert decision.input_manifest_hash == sample_composite_score.agent_bundle_sha256


def test_composite_score_with_market_index() -> None:
    """测试带市场指数的 CompositeScore 转换"""
    market_snapshot = MarketIndexSnapshot(
        trading_date=date(2026, 7, 17),
        composite_return_1d=0.01,
        composite_return_5d=0.02,
        composite_return_20d=0.05,
        regime="RISK_ON",
        score_adjustment=3.0,
        risk_multiplier=1.0,
    )

    composite_score = CompositeScore(
        symbol="600000.SH",
        trading_date=date(2026, 7, 17),
        decision_at=datetime(2026, 7, 17, 15, 30, tzinfo=timezone.utc),
        fundamental_score=70.0,
        technical_score=65.0,
        sentiment_score=68.0,
        quality_confidence_score=80.0,
        total_score=72.0,
        formula_version="composite-35-35-20-10-dividend-news-market-v3",
        agent_bundle_sha256="c" * 64,
        feature_snapshot_id="00000000-0000-0000-0000-000000000002",
        evidence_bundle_sha256="d" * 64,
        dividend_bonus=2.0,
        event_risk_multiplier=0.95,
        market_index_snapshot=market_snapshot,
        market_regime="RISK_ON",
        market_score_adjustment=3.0,
        market_risk_multiplier=1.0,
    )

    market_state = MarketState(
        symbol="600000.SH",
        trading_date=date(2026, 7, 17),
        available_at=datetime(2026, 7, 17, 17, 0, tzinfo=timezone.utc),
        open=10.0,
        high=10.2,
        low=9.9,
        close=10.1,
        volume=1000000.0,
        amount=10000000.0,
    )

    decision = convert_composite_score_to_decision(
        composite_score=composite_score,
        market_state=market_state,
    )

    # 市场指数信息不影响 Legacy Provider 的动作映射
    # 但应该正确传递 total_score
    assert decision.symbol == "600000.SH"
    assert decision.action == "BUY"  # score=72 -> BUY


def test_legacy_provider_score_boundaries() -> None:
    """测试 LegacyDecisionProvider 的评分边界"""
    provider = LegacyDecisionProvider()

    # 测试高分边界
    action, probs = provider._score_to_action(80.0)
    assert action == "BUY"
    assert probs.BUY > probs.HOLD

    # 测试中分边界
    action, probs = provider._score_to_action(60.0)
    assert action == "HOLD"
    assert probs.HOLD > probs.BUY

    # 测试低分边界
    action, probs = provider._score_to_action(40.0)
    assert action == "SELL"
    assert probs.SELL > probs.HOLD

    # 测试仓位映射
    assert provider._score_to_position(85.0) == 70
    assert provider._score_to_position(75.0) == 50
    assert provider._score_to_position(65.0) == 30
    assert provider._score_to_position(55.0) == 20
    assert provider._score_to_position(45.0) == 10
    assert provider._score_to_position(35.0) == 0
