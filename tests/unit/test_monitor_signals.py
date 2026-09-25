from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ashare_ai.api.schemas import MonitorSignalResponse, MonitorSignalsResponse
from ashare_ai.features.signals import detect_monitor_signals


def _bars(closes: list[float], volumes: list[float]) -> list[dict[str, object]]:
    start = datetime(2026, 9, 25, 9, 30, tzinfo=UTC)
    return [
        {
            "timestamp": (start + timedelta(minutes=index)).isoformat(),
            "close": close,
            "volume": volume,
        }
        for index, (close, volume) in enumerate(zip(closes, volumes, strict=True))
    ]


def test_monitor_signals_emit_all_four_deterministic_types(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASHARE_NATIVE_TECHNICAL", "off")
    decision_at = datetime(2026, 9, 25, 18, 0, tzinfo=UTC)
    cases = [
        ([10.0, 10.0, 10.0, 9.5], [100.0] * 4),
        ([10.0] * 20 + [10.2], [100.0] * 20 + [200.0]),
        ([10.0] * 5 + [10.4] * 5, [100.0] * 5 + [70.0] * 5),
        ([10.0] * 15 + [11.0] * 5 + [0.1], [100.0] * 21),
    ]
    signals = [
        signal
        for closes, volumes in cases
        for signal in detect_monitor_signals(
            symbol="600519.SH",
            bars=_bars(closes, volumes),
            available_at=datetime(2026, 9, 25, 16, 0, tzinfo=UTC),
            decision_at=decision_at,
        )
    ]

    signal_types = {signal.signal_type for signal in signals}
    assert {
        "INTRADAY_DROP",
        "VOLUME_BREAKOUT",
        "VOLUME_PRICE_DIVERGENCE",
        "MA_DEATH_CROSS",
    } <= signal_types
    assert all(signal.symbol == "600519.SH" for signal in signals)
    assert all(signal.available_at <= signal.decision_at for signal in signals)
    assert all(0 <= signal.confidence <= 1 for signal in signals)


def test_monitor_signals_drop_future_bars_and_validate_pit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASHARE_NATIVE_TECHNICAL", "off")
    decision_at = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
    start = datetime(2026, 9, 25, 9, 55, tzinfo=UTC)
    bars = [
        {"timestamp": (start + timedelta(minutes=index)).isoformat(), "close": 10.0, "volume": 100.0}
        for index in range(4)
    ]
    bars.append({"timestamp": "2026-09-25T10:05:00+00:00", "close": 8.0, "volume": 100.0})

    signals = detect_monitor_signals(
        symbol="000001.SZ",
        bars=bars,
        available_at=datetime(2026, 9, 25, 9, 59, tzinfo=UTC),
        decision_at=decision_at,
    )
    assert signals == []

    with pytest.raises(ValueError, match="available_at must not be after decision_at"):
        detect_monitor_signals(
            symbol="000001.SZ",
            bars=[],
            available_at=datetime(2026, 9, 25, 10, 1, tzinfo=UTC),
            decision_at=decision_at,
        )


def test_monitor_signal_api_contract_keeps_pit_and_evidence_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ASHARE_NATIVE_TECHNICAL", "off")
    decision_at = datetime(2026, 9, 25, 18, 0, tzinfo=UTC)
    signal = detect_monitor_signals(
        symbol="600000.SH",
        bars=_bars([10.0, 10.0, 10.0, 9.5], [100.0] * 4),
        available_at=datetime(2026, 9, 25, 15, 0, tzinfo=UTC),
        decision_at=decision_at,
    )[0]
    response = MonitorSignalsResponse(
        items=[MonitorSignalResponse.model_validate(signal.model_dump(mode="json"))],
        generated_at=decision_at,
        decision_at=decision_at,
    )
    item = response.items[0]
    assert item.symbol == "600000.SH"
    assert item.trading_date.isoformat() == "2026-09-25"
    assert item.available_at <= item.decision_at
    assert item.signal_type == "INTRADAY_DROP"
    assert "primary" in item.evidence
