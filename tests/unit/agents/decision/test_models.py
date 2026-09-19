from __future__ import annotations

import pytest
from datetime import date, datetime, timezone

from ashare_ai.agents.decision.models import (
    ActionProbabilities,
    BinaryProbabilities,
    DecisionProbabilities,
    DirectionProbabilities,
    MarketState,
    RiskProbabilities,
    UnifiedDecision,
)


class TestProbabilityModels:
    """测试概率分布模型的和为 1 校验"""

    def test_direction_probabilities_sum_to_one(self):
        # 正常情况
        probs = DirectionProbabilities(UP=0.5, FLAT=0.3, DOWN=0.2)
        assert abs(probs.UP + probs.FLAT + probs.DOWN - 1.0) < 1e-6

    def test_direction_probabilities_invalid_sum(self):
        # 概率和不为 1
        with pytest.raises(ValueError, match="must sum to 1"):
            DirectionProbabilities(UP=0.5, FLAT=0.3, DOWN=0.3)

    def test_binary_probabilities_sum_to_one(self):
        probs = BinaryProbabilities(YES=0.6, NO=0.4)
        assert abs(probs.YES + probs.NO - 1.0) < 1e-6

    def test_binary_probabilities_invalid_sum(self):
        with pytest.raises(ValueError, match="must sum to 1"):
            BinaryProbabilities(YES=0.7, NO=0.4)

    def test_action_probabilities_sum_to_one(self):
        probs = ActionProbabilities(BUY=0.5, HOLD=0.3, SELL=0.2)
        assert abs(probs.BUY + probs.HOLD + probs.SELL - 1.0) < 1e-6

    def test_action_probabilities_invalid_sum(self):
        with pytest.raises(ValueError, match="must sum to 1"):
            ActionProbabilities(BUY=0.6, HOLD=0.3, SELL=0.3)

    def test_risk_probabilities_sum_to_one(self):
        probs = RiskProbabilities(LOW=0.3, MEDIUM=0.5, HIGH=0.2)
        assert abs(probs.LOW + probs.MEDIUM + probs.HIGH - 1.0) < 1e-6

    def test_risk_probabilities_invalid_sum(self):
        with pytest.raises(ValueError, match="must sum to 1"):
            RiskProbabilities(LOW=0.5, MEDIUM=0.5, HIGH=0.5)


class TestUnifiedDecision:
    """测试统一决策模型的时间和哈希校验"""

    def test_valid_decision(self):
        available_at = datetime(2026, 7, 17, 15, 0, tzinfo=timezone.utc)
        decision_at = datetime(2026, 7, 17, 18, 0, tzinfo=timezone.utc)

        decision = UnifiedDecision(
            symbol="600000.SH",
            trading_date=date(2026, 7, 17),
            available_at=available_at,
            decision_at=decision_at,
            probabilities=DecisionProbabilities(
                direction_1d=DirectionProbabilities(UP=0.5, FLAT=0.3, DOWN=0.2),
                direction_5d=DirectionProbabilities(UP=0.55, FLAT=0.3, DOWN=0.15),
                up_over_3pct_5d=BinaryProbabilities(YES=0.45, NO=0.55),
                action=ActionProbabilities(BUY=0.6, HOLD=0.3, SELL=0.1),
                risk=RiskProbabilities(LOW=0.4, MEDIUM=0.5, HIGH=0.1),
            ),
            action="BUY",
            risk="MEDIUM",
            position=30,
            mode="legacy",
            model_version="legacy-v1.0.0",
            input_manifest_hash="a" * 64,
        )

        assert decision.symbol == "600000.SH"
        assert decision.action == "BUY"
        assert decision.position == 30

    def test_decision_at_date_mismatch(self):
        available_at = datetime(2026, 7, 17, 15, 0, tzinfo=timezone.utc)
        decision_at = datetime(2026, 7, 18, 18, 0, tzinfo=timezone.utc)  # 不同日期

        with pytest.raises(ValueError, match="decision_at must fall on trading_date"):
            UnifiedDecision(
                symbol="600000.SH",
                trading_date=date(2026, 7, 17),
                available_at=available_at,
                decision_at=decision_at,
                probabilities=DecisionProbabilities(
                    direction_1d=DirectionProbabilities(UP=0.5, FLAT=0.3, DOWN=0.2),
                    direction_5d=DirectionProbabilities(UP=0.55, FLAT=0.3, DOWN=0.15),
                    up_over_3pct_5d=BinaryProbabilities(YES=0.45, NO=0.55),
                    action=ActionProbabilities(BUY=0.6, HOLD=0.3, SELL=0.1),
                    risk=RiskProbabilities(LOW=0.4, MEDIUM=0.5, HIGH=0.1),
                ),
                action="BUY",
                risk="MEDIUM",
                position=30,
                mode="legacy",
                model_version="legacy-v1.0.0",
                input_manifest_hash="a" * 64,
            )

    def test_available_at_after_decision_at(self):
        available_at = datetime(2026, 7, 17, 19, 0, tzinfo=timezone.utc)
        decision_at = datetime(2026, 7, 17, 18, 0, tzinfo=timezone.utc)

        with pytest.raises(ValueError, match="available_at must not be after decision_at"):
            UnifiedDecision(
                symbol="600000.SH",
                trading_date=date(2026, 7, 17),
                available_at=available_at,
                decision_at=decision_at,
                probabilities=DecisionProbabilities(
                    direction_1d=DirectionProbabilities(UP=0.5, FLAT=0.3, DOWN=0.2),
                    direction_5d=DirectionProbabilities(UP=0.55, FLAT=0.3, DOWN=0.15),
                    up_over_3pct_5d=BinaryProbabilities(YES=0.45, NO=0.55),
                    action=ActionProbabilities(BUY=0.6, HOLD=0.3, SELL=0.1),
                    risk=RiskProbabilities(LOW=0.4, MEDIUM=0.5, HIGH=0.1),
                ),
                action="BUY",
                risk="MEDIUM",
                position=30,
                mode="legacy",
                model_version="legacy-v1.0.0",
                input_manifest_hash="a" * 64,
            )

    def test_invalid_hash_format(self):
        available_at = datetime(2026, 7, 17, 15, 0, tzinfo=timezone.utc)
        decision_at = datetime(2026, 7, 17, 18, 0, tzinfo=timezone.utc)

        # Pydantic 的 Field 约束先于自定义 validator 运行，所以会触发 ValidationError
        with pytest.raises((ValueError, Exception), match="must be 64-character hex SHA-256|at least 64 characters"):
            UnifiedDecision(
                symbol="600000.SH",
                trading_date=date(2026, 7, 17),
                available_at=available_at,
                decision_at=decision_at,
                probabilities=DecisionProbabilities(
                    direction_1d=DirectionProbabilities(UP=0.5, FLAT=0.3, DOWN=0.2),
                    direction_5d=DirectionProbabilities(UP=0.55, FLAT=0.3, DOWN=0.15),
                    up_over_3pct_5d=BinaryProbabilities(YES=0.45, NO=0.55),
                    action=ActionProbabilities(BUY=0.6, HOLD=0.3, SELL=0.1),
                    risk=RiskProbabilities(LOW=0.4, MEDIUM=0.5, HIGH=0.1),
                ),
                action="BUY",
                risk="MEDIUM",
                position=30,
                mode="legacy",
                model_version="legacy-v1.0.0",
                input_manifest_hash="invalid",  # 非 64 位 hex
            )

    def test_invalid_hash_non_hex(self):
        available_at = datetime(2026, 7, 17, 15, 0, tzinfo=timezone.utc)
        decision_at = datetime(2026, 7, 17, 18, 0, tzinfo=timezone.utc)

        # 64 位但包含非 hex 字符
        with pytest.raises(ValueError, match="must be 64-character hex SHA-256"):
            UnifiedDecision(
                symbol="600000.SH",
                trading_date=date(2026, 7, 17),
                available_at=available_at,
                decision_at=decision_at,
                probabilities=DecisionProbabilities(
                    direction_1d=DirectionProbabilities(UP=0.5, FLAT=0.3, DOWN=0.2),
                    direction_5d=DirectionProbabilities(UP=0.55, FLAT=0.3, DOWN=0.15),
                    up_over_3pct_5d=BinaryProbabilities(YES=0.45, NO=0.55),
                    action=ActionProbabilities(BUY=0.6, HOLD=0.3, SELL=0.1),
                    risk=RiskProbabilities(LOW=0.4, MEDIUM=0.5, HIGH=0.1),
                ),
                action="BUY",
                risk="MEDIUM",
                position=30,
                mode="legacy",
                model_version="legacy-v1.0.0",
                input_manifest_hash="z" * 64,  # 非 hex 字符
            )


class TestMarketState:
    """测试 MarketState 的 OHLC 和字段校验"""

    def test_valid_market_state(self):
        state = MarketState(
            symbol="600000.SH",
            trading_date=date(2026, 7, 17),
            available_at=datetime(2026, 7, 17, 15, 0, tzinfo=timezone.utc),
            open=10.0,
            high=10.5,
            low=9.8,
            close=10.2,
            volume=1000000.0,
            amount=10000000.0,
        )

        assert state.symbol == "600000.SH"
        assert state.close == 10.2

    def test_invalid_ohlc_high_too_low(self):
        with pytest.raises(ValueError, match="high is below OHLC values"):
            MarketState(
                symbol="600000.SH",
                trading_date=date(2026, 7, 17),
                available_at=datetime(2026, 7, 17, 15, 0, tzinfo=timezone.utc),
                open=10.0,
                high=9.5,  # high 低于 open
                low=9.8,
                close=10.2,
                volume=1000000.0,
                amount=10000000.0,
            )

    def test_invalid_ohlc_low_too_high(self):
        # low > high 会触发 high < low 的校验，错误信息是 "high is below OHLC values"
        with pytest.raises(ValueError, match="high is below OHLC values"):
            MarketState(
                symbol="600000.SH",
                trading_date=date(2026, 7, 17),
                available_at=datetime(2026, 7, 17, 15, 0, tzinfo=timezone.utc),
                open=10.0,
                high=10.5,
                low=10.6,  # low 高于 high
                close=10.2,
                volume=1000000.0,
                amount=10000000.0,
            )
