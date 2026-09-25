from __future__ import annotations

from typing import Any

import httpx

from ashare_ai.agents.decision.models import MarketState, UnifiedDecision
from ashare_ai.agents.decision.protocols import DecisionProvider
from ashare_ai.core.hashing import stable_hash


class JevLiveDecisionProvider(DecisionProvider):
    """Jev Live adapter with strict response and PIT validation."""

    def __init__(self, base_url: str, api_key: str, model: str = "jev-live", endpoint: str = "/v1/decisions", timeout_seconds: float = 30.0):
        if not base_url or not api_key:
            raise ValueError("Jev Live requires JEV_LIVE_BASE_URL and JEV_LIVE_API_KEY")
        self._url = base_url.rstrip("/") + "/" + endpoint.lstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds

    @property
    def mode(self) -> str:
        return "jev"

    @property
    def model_version(self) -> str:
        return self._model

    async def predict(self, market_state: MarketState) -> UnifiedDecision:
        payload = {"model": self._model, "market_state": market_state.model_dump(mode="json")}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(self._url, json=payload, headers={"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"})
                response.raise_for_status()
                body: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(f"Jev Live request failed: {type(exc).__name__}") from exc
        raw = body.get("decision", body) if isinstance(body, dict) else body
        if isinstance(raw, dict) and not raw.get("input_manifest_hash"):
            raw = {**raw, "input_manifest_hash": stable_hash(market_state.model_dump())}
        decision = UnifiedDecision.model_validate(raw)
        if decision.symbol != market_state.symbol or decision.trading_date != market_state.trading_date:
            raise ValueError("Jev Live response identity does not match MarketState")
        if decision.available_at > decision.decision_at or decision.available_at > market_state.available_at:
            raise ValueError("Jev Live response violates point-in-time constraints")
        return decision
