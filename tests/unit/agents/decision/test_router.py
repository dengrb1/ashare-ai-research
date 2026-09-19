from __future__ import annotations

import pytest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from ashare_ai.agents.decision.models import (
    ActionProbabilities,
    BinaryProbabilities,
    DecisionProbabilities,
    DirectionProbabilities,
    MarketState,
    RiskProbabilities,
    UnifiedDecision,
)
from ashare_ai.agents.decision.router import DecisionRouter


@pytest.mark.asyncio
class TestDecisionRouter:
    """测试决策路由器的模式切换和 fallback 逻辑"""

    async def test_router_legacy_mode(self):
        router = DecisionRouter(mode="legacy", fallback_enabled=False)

        market_state = MarketState(
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

        decision = await router.predict(market_state)

        assert decision.mode == "legacy"
        assert decision.symbol == "600000.SH"

    async def test_router_invalid_mode(self):
        with pytest.raises(ValueError, match="Unsupported decision mode"):
            router = DecisionRouter(mode="invalid_mode", fallback_enabled=False)  # type: ignore
            await router.predict(
                MarketState(
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
            )

    async def test_router_jev_fails_without_fallback(self):
        router = DecisionRouter(
            mode="jev",
            fallback_enabled=False,
            jev_model_dir=Path("data/models/jev"),
            jev_model_version="nonexistent",
        )

        market_state = MarketState(
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

        # Jev 失败且 fallback 未开启，应该抛出 RuntimeError
        with pytest.raises(RuntimeError, match="failed and fallback is disabled"):
            await router.predict(market_state)

    async def test_router_jev_fails_with_fallback(self):
        router = DecisionRouter(
            mode="jev",
            fallback_enabled=True,
            jev_model_dir=Path("data/models/jev"),
            jev_model_version="nonexistent",
        )

        market_state = MarketState(
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

        # Jev 失败但 fallback 开启，应该回退到 Legacy
        decision = await router.predict(market_state)

        assert decision.mode == "legacy"
        assert decision.symbol == "600000.SH"

    @patch("ashare_ai.agents.decision.router.DecisionRouter._get_provider")
    async def test_router_both_providers_fail(self, mock_get_provider):
        # Mock 主 Provider 和 fallback Provider 都失败
        mock_primary = AsyncMock()
        mock_primary.predict.side_effect = RuntimeError("Primary failed")

        mock_fallback = AsyncMock()
        mock_fallback.predict.side_effect = RuntimeError("Fallback failed")

        mock_get_provider.side_effect = [mock_primary, mock_fallback]

        router = DecisionRouter(mode="jev", fallback_enabled=True)

        market_state = MarketState(
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

        # 两者都失败应该抛出 RuntimeError
        with pytest.raises(RuntimeError, match="Both primary.*and fallback.*providers failed"):
            await router.predict(market_state)
