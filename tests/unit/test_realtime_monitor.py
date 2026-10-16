from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

from ashare_ai.api import app as app_module
from ashare_ai.api.dependencies import get_auth_context
from ashare_ai.monitoring.realtime import assess_position, build_live_snapshot


def _position(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "symbol": "600519.SH",
        "name": "贵州茅台",
        "quantity": 100,
        "cost": 100,
        "stop_loss_price": 95,
        "stop_loss_enabled": True,
    }
    value.update(overrides)
    return value


def _quote(price: float, collected_at: datetime) -> dict[str, object]:
    return {
        "symbol": "600519.SH",
        "price": price,
        "status": {"source": "fixture", "collected_at": collected_at.isoformat()},
    }


def test_assess_position_emits_triggered_stop_and_pit_metadata() -> None:
    decision_at = datetime(2026, 9, 27, 3, 0, tzinfo=UTC)
    result = assess_position(
        _position(),
        _quote(94.5, decision_at - timedelta(seconds=2)),
        decision_at=decision_at,
    )

    assert result["risk_state"] == "STOP_LOSS_TRIGGERED"
    assert result["risk_level"] == "CRITICAL"
    assert result["available_at"] <= result["decision_at"]
    assert result["strategy_version"] == "realtime-position-v1"


def test_assess_position_uses_trailing_stop_after_profit() -> None:
    decision_at = datetime(2026, 9, 27, 3, 0, tzinfo=UTC)
    result = assess_position(
        _position(high_water_price=120),
        _quote(110, decision_at),
        decision_at=decision_at,
    )

    assert result["trailing_stop_price"] == 111.6
    assert result["risk_state"] == "TRAILING_STOP_TRIGGERED"


def test_assess_position_uses_provider_intraday_high_for_trailing_stop() -> None:
    decision_at = datetime(2026, 9, 27, 3, 0, tzinfo=UTC)
    quote = _quote(110, decision_at)
    quote["high"] = 120
    result = assess_position(_position(), quote, decision_at=decision_at)

    assert result["trailing_stop_price"] == 111.6
    assert result["risk_state"] == "TRAILING_STOP_TRIGGERED"


def test_assess_position_marks_delayed_quote_as_data_stale() -> None:
    decision_at = datetime(2026, 9, 27, 3, 0, tzinfo=UTC)
    result = assess_position(
        _position(),
        _quote(101, decision_at - timedelta(minutes=2)),
        decision_at=decision_at,
    )

    assert result["risk_state"] == "DATA_STALE"
    assert result["quote_status"]["stale"] is True


def test_assess_position_clamps_provider_clock_skew_to_decision_time() -> None:
    decision_at = datetime(2026, 9, 27, 3, 0, tzinfo=UTC)
    result = assess_position(
        _position(),
        _quote(101, decision_at + timedelta(minutes=5)),
        decision_at=decision_at,
    )

    assert result["available_at"] == result["decision_at"]


def test_build_live_snapshot_bounds_symbols_and_keeps_research_isolated(monkeypatch) -> None:
    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        "ashare_ai.monitoring.realtime.UserAssetService.get",
        lambda _service, _user_id: {
            "positions": [_position()],
            "watchlist": ["600519.SH"],
            "market_refresh_interval_seconds": 5,
        },
    )
    monkeypatch.setattr(
        "ashare_ai.monitoring.realtime._calendar_sessions",
        lambda _now: set(),
    )

    class Market:
        def quotes(self, symbols, *, force_refresh=False):
            assert symbols == ["600519.SH"]
            assert force_refresh is False
            return [_quote(101, datetime(2026, 9, 27, 3, 0, tzinfo=UTC))]

    payload = build_live_snapshot(
        "user-1",
        now=datetime(2026, 9, 27, 3, 0, tzinfo=UTC),
        market=Market(),
        session_factory=Session,
    )

    assert payload["research_snapshot_isolated"] is True
    assert len(payload["positions"]) == 1
    assert payload["session"]["state"] == "CLOSED"


def test_mobile_live_monitor_endpoint_returns_shared_snapshot_contract(monkeypatch) -> None:
    now = datetime(2026, 9, 27, 3, 0, tzinfo=UTC)
    snapshot = {
        "monitor_version": "realtime-position-v1",
        "generated_at": now.isoformat(),
        "decision_at": now.isoformat(),
        "session": {"state": "CLOSED", "is_trading_day": False, "reason": "NON_TRADING_DAY"},
        "next_poll_seconds": 5,
        "positions": [
            assess_position(
                _position(), _quote(94, now), decision_at=now
            )
        ],
        "watchlist_quotes": [],
        "signals": [],
        "market_error": None,
        "stale": False,
        "research_snapshot_isolated": True,
    }
    monkeypatch.setattr(app_module, "build_live_snapshot", lambda *_args, **_kwargs: snapshot)
    app_module.app.dependency_overrides[get_auth_context] = lambda: SimpleNamespace(
        user=SimpleNamespace(user_id="mobile-user")
    )
    try:
        client = TestClient(app_module.app)
        response = client.get("/api/v1/mobile/monitor/live")
    finally:
        app_module.app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload["positions"][0]["risk_state"] == "STOP_LOSS_TRIGGERED"
    assert payload["positions"][0]["available_at"] <= payload["positions"][0]["decision_at"]
    assert payload["research_snapshot_isolated"] is True
