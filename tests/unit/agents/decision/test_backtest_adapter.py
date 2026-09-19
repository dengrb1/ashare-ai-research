from __future__ import annotations

import pytest
from datetime import date, datetime, timezone
from decimal import Decimal

from ashare_ai.agents.decision.backtest_adapter import (
    decision_to_signal,
    enrich_signal_with_decision_metadata,
)
from ashare_ai.agents.decision.models import (
    ActionProbabilities,
    BinaryProbabilities,
    DecisionProbabilities,
    DirectionProbabilities,
    RiskProbabilities,
    UnifiedDecision,
)


class TestBacktestAdapter:
    """测试 UnifiedDecision 到 BacktestSignal 的转换"""

    def test_decision_to_signal_position_mapping(self):
        """测试 position 枚举正确转换为 target_weight"""
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

        signal = decision_to_signal(
            decision=decision,
            snapshot_hash="b" * 64,
            industry_code="801010",
        )

        assert signal.symbol == "600000.SH"
        assert signal.signal_date == date(2026, 7, 17)
        assert signal.decision_at == decision_at
        assert signal.target_weight == Decimal("0.30")
        assert signal.snapshot_hash == "b" * 64
        assert signal.industry_code == "801010"

    def test_decision_to_signal_zero_position(self):
        """测试 position=0 转换为 target_weight=0.0"""
        available_at = datetime(2026, 7, 17, 15, 0, tzinfo=timezone.utc)
        decision_at = datetime(2026, 7, 17, 18, 0, tzinfo=timezone.utc)

        decision = UnifiedDecision(
            symbol="600000.SH",
            trading_date=date(2026, 7, 17),
            available_at=available_at,
            decision_at=decision_at,
            probabilities=DecisionProbabilities(
                direction_1d=DirectionProbabilities(UP=0.2, FLAT=0.3, DOWN=0.5),
                direction_5d=DirectionProbabilities(UP=0.25, FLAT=0.25, DOWN=0.5),
                up_over_3pct_5d=BinaryProbabilities(YES=0.25, NO=0.75),
                action=ActionProbabilities(BUY=0.1, HOLD=0.3, SELL=0.6),
                risk=RiskProbabilities(LOW=0.05, MEDIUM=0.35, HIGH=0.6),
            ),
            action="SELL",
            risk="HIGH",
            position=0,
            mode="jev",
            model_version="jev-baseline-v1",
            input_manifest_hash="c" * 64,
        )

        signal = decision_to_signal(
            decision=decision,
            snapshot_hash="d" * 64,
            industry_code="801020",
        )

        assert signal.target_weight == Decimal("0.00")

    def test_decision_to_signal_full_position(self):
        """测试 position=100 转换为 target_weight=1.0"""
        available_at = datetime(2026, 7, 17, 15, 0, tzinfo=timezone.utc)
        decision_at = datetime(2026, 7, 17, 18, 0, tzinfo=timezone.utc)

        decision = UnifiedDecision(
            symbol="600000.SH",
            trading_date=date(2026, 7, 17),
            available_at=available_at,
            decision_at=decision_at,
            probabilities=DecisionProbabilities(
                direction_1d=DirectionProbabilities(UP=0.8, FLAT=0.15, DOWN=0.05),
                direction_5d=DirectionProbabilities(UP=0.85, FLAT=0.1, DOWN=0.05),
                up_over_3pct_5d=BinaryProbabilities(YES=0.75, NO=0.25),
                action=ActionProbabilities(BUY=0.8, HOLD=0.15, SELL=0.05),
                risk=RiskProbabilities(LOW=0.7, MEDIUM=0.25, HIGH=0.05),
            ),
            action="BUY",
            risk="LOW",
            position=100,
            mode="legacy",
            model_version="legacy-v1.0.0",
            input_manifest_hash="e" * 64,
        )

        signal = decision_to_signal(
            decision=decision,
            snapshot_hash="f" * 64,
            industry_code="801030",
        )

        assert signal.target_weight == Decimal("1.00")

    def test_enrich_signal_with_decision_metadata(self):
        """测试信号元数据扩展"""
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
            mode="jev",
            model_version="jev-baseline-v1",
            input_manifest_hash="0123456789abcdef" * 4,  # 64位有效 hex
        )

        signal = decision_to_signal(
            decision=decision,
            snapshot_hash="fedcba9876543210" * 4,  # 64位有效 hex
            industry_code="801010",
        )

        enriched = enrich_signal_with_decision_metadata(signal, decision)

        # 校验原始信号字段
        assert enriched["symbol"] == "600000.SH"
        assert enriched["target_weight"] == Decimal("0.30")

        # 校验扩展的决策字段
        assert enriched["decision_mode"] == "jev"
        assert enriched["model_version"] == "jev-baseline-v1"
        assert enriched["action"] == "BUY"
        assert enriched["risk"] == "MEDIUM"
        assert enriched["position"] == 30

        # 校验概率分布
        assert enriched["action_probabilities"]["BUY"] == 0.6
        assert enriched["risk_probabilities"]["MEDIUM"] == 0.5
        assert enriched["direction_1d"]["UP"] == 0.5
        assert enriched["direction_5d"]["UP"] == 0.55
        assert enriched["up_over_3pct_5d"]["YES"] == 0.45

        assert enriched["input_manifest_hash"] == "0123456789abcdef" * 4
