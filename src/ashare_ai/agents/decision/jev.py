from __future__ import annotations

import json
import logging
import pickle
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ashare_ai.agents.decision.models import (
    ActionProbabilities,
    BinaryProbabilities,
    DecisionProbabilities,
    DirectionProbabilities,
    RiskProbabilities,
    UnifiedDecision,
)
from ashare_ai.agents.decision.protocols import DecisionProvider
from ashare_ai.core.hashing import stable_hash

if TYPE_CHECKING:
    from ashare_ai.agents.decision.models import MarketState

logger = logging.getLogger(__name__)


class JevDecisionProvider(DecisionProvider):
    """Local Jev System-1 provider with a deterministic lightweight baseline."""

    def __init__(self, model_dir: Path, model_version: str, checkpoint_path: Path | None = None, device: str = "auto"):
        self._model_dir = model_dir
        self._model_version = model_version
        self._checkpoint_path = checkpoint_path
        self._device = device
        self._model: Any | None = None
        self._loaded = False

    @property
    def mode(self) -> str:
        return "jev"

    @property
    def model_version(self) -> str:
        return self._model_version

    def _checkpoint(self) -> Path:
        return self._checkpoint_path or self._model_dir / self._model_version / "checkpoint.pt"

    def _load_model(self) -> None:
        if self._loaded:
            return
        path = self._checkpoint()
        if not path.exists():
            raise FileNotFoundError(f"Jev checkpoint not found: {path}. Train the model first using: ashare-ai train-jev")
        metadata: dict[str, Any] = {}
        try:
            if path.suffix == ".json":
                metadata = json.loads(path.read_text(encoding="utf-8"))
            else:
                import torch

                loaded = torch.load(path, map_location="cpu", weights_only=True)
                if isinstance(loaded, dict):
                    metadata = dict(loaded.get("metadata") or {})
        except ImportError:
            metadata = {"backend": "deterministic-baseline"}
        except (OSError, ValueError, RuntimeError, pickle.UnpicklingError) as exc:
            raise RuntimeError(f"Jev checkpoint could not be loaded: {type(exc).__name__}") from exc
        self._model = {"metadata": metadata, "device": self._device}
        self._loaded = True

    @staticmethod
    def _probabilities(state: MarketState) -> tuple[float, float, float]:
        close = state.close
        trend = ((close - (state.ma20 or close)) / close) if close else 0.0
        momentum = (state.macd_hist or 0.0) / max(close, 1e-9)
        score = max(-1.0, min(1.0, trend * 4.0 + momentum * 20.0))
        up = 0.34 + 0.28 * max(score, 0.0)
        down = 0.34 + 0.28 * max(-score, 0.0)
        flat = max(0.02, 1.0 - up - down)
        total = up + flat + down
        return up / total, flat / total, down / total

    async def predict(self, market_state: MarketState) -> UnifiedDecision:
        self._load_model()
        up, flat, down = self._probabilities(market_state)
        direction_1d = DirectionProbabilities(UP=up, FLAT=flat, DOWN=down)
        direction_5d = DirectionProbabilities(UP=(up + 0.06) / 1.06, FLAT=flat / 1.06, DOWN=down / 1.06)
        yes = min(0.95, max(0.05, up * 0.9))
        up3 = BinaryProbabilities(YES=yes, NO=1.0 - yes)
        buy = min(0.9, max(0.05, 0.25 + up * 0.75))
        sell = min(0.9, max(0.05, 0.25 + down * 0.75))
        hold = max(0.02, 1.0 - buy - sell)
        total = buy + hold + sell
        action_probs = ActionProbabilities(BUY=buy / total, HOLD=hold / total, SELL=sell / total)
        risk_high = 0.6 if market_state.market_regime == "RISK_OFF" else 0.18
        risk_low = 0.15 if risk_high > 0.5 else 0.45
        risk_probs = RiskProbabilities(LOW=risk_low, MEDIUM=1.0 - risk_low - risk_high, HIGH=risk_high)
        action = max((("BUY", action_probs.BUY), ("HOLD", action_probs.HOLD), ("SELL", action_probs.SELL)), key=lambda item: item[1])[0]
        risk = max((("LOW", risk_probs.LOW), ("MEDIUM", risk_probs.MEDIUM), ("HIGH", risk_probs.HIGH)), key=lambda item: item[1])[0]
        position = 50 if action == "BUY" and risk == "LOW" else 30 if action == "BUY" else 0 if action == "SELL" else 20
        return UnifiedDecision(
            symbol=market_state.symbol,
            trading_date=market_state.trading_date,
            available_at=market_state.available_at,
            decision_at=market_state.available_at,
            probabilities=DecisionProbabilities(direction_1d=direction_1d, direction_5d=direction_5d, up_over_3pct_5d=up3, action=action_probs, risk=risk_probs),
            action=action,
            risk=risk,
            position=position,
            mode="jev",
            model_version=self._model_version,
            input_manifest_hash=stable_hash(market_state.model_dump()),
        )
