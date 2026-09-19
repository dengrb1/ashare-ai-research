from __future__ import annotations

import pytest
from datetime import date, datetime, timezone
from pathlib import Path

from ashare_ai.agents.decision.jev import JevDecisionProvider
from ashare_ai.agents.decision.models import MarketState


@pytest.mark.asyncio
class TestJevDecisionProvider:
    """测试 Jev Provider 的加载和输出"""

    async def test_jev_provider_mode_property(self):
        provider = JevDecisionProvider(
            model_dir=Path("data/models/jev"),
            model_version="jev-baseline-v1",
            device="cpu",
        )
        assert provider.mode == "jev"

    async def test_jev_provider_model_version(self):
        provider = JevDecisionProvider(
            model_dir=Path("data/models/jev"),
            model_version="jev-test-v1",
            device="cpu",
        )
        assert provider.model_version == "jev-test-v1"

    async def test_jev_provider_checkpoint_not_found(self):
        provider = JevDecisionProvider(
            model_dir=Path("data/models/jev"),
            model_version="nonexistent-model",
            device="cpu",
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

        # PyTorch 未安装会抛出 RuntimeError，安装后会抛出 FileNotFoundError
        with pytest.raises((FileNotFoundError, RuntimeError), match="Jev checkpoint not found|PyTorch is not installed"):
            await provider.predict(market_state)

    async def test_jev_provider_skeleton_implementation(self):
        """测试骨架实现在模型未加载时的行为"""
        provider = JevDecisionProvider(
            model_dir=Path("data/models/jev"),
            model_version="jev-baseline-v1",
            device="cpu",
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

        # 骨架实现会抛出 RuntimeError（模型未训练/PyTorch 未安装）
        # 或 FileNotFoundError（checkpoint 不存在）
        with pytest.raises((RuntimeError, FileNotFoundError)):
            await provider.predict(market_state)
