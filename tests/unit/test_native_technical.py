from __future__ import annotations

import math
import statistics
from itertools import pairwise
from types import SimpleNamespace

import pytest

from ashare_ai.features import native as native_module


def _reference_metrics(
    closes: list[float], volumes: list[float]
) -> tuple[float, float, float, float, float, float]:
    returns = [current / previous - 1 for previous, current in pairwise(closes)]
    average = statistics.fmean(closes[-20:])
    peak = closes[-60]
    drawdown = 0.0
    for value in closes[-60:]:
        peak = max(peak, value)
        drawdown = min(drawdown, value / peak - 1)
    return (
        closes[-1] / closes[-6] - 1,
        closes[-1] / closes[-21] - 1,
        closes[-1] / average - 1,
        statistics.pstdev(returns[-20:]) * math.sqrt(252),
        statistics.fmean(volumes[-5:]) / statistics.fmean(volumes[-20:]),
        drawdown,
    )


def test_native_technical_facade_returns_extension_payload(monkeypatch) -> None:
    closes = [100.0 + index * 0.5 + (index % 3) for index in range(65)]
    volumes = [1_000.0 + index * 10 for index in range(65)]
    expected = _reference_metrics(closes, volumes)
    fake_module = SimpleNamespace(calculate_technical_metrics=lambda _c, _v: expected)
    monkeypatch.setenv("ASHARE_NATIVE_TECHNICAL", "on")
    monkeypatch.setattr(native_module, "_load_native_module", lambda: fake_module)

    assert native_module.native_technical_metrics(closes, volumes) == expected


def test_native_technical_facade_falls_back_or_fails_explicitly(monkeypatch) -> None:
    monkeypatch.setattr(native_module, "_load_native_module", lambda: None)
    monkeypatch.setenv("ASHARE_NATIVE_TECHNICAL", "auto")
    assert native_module.native_technical_metrics([1.0], [1.0]) is None

    monkeypatch.setenv("ASHARE_NATIVE_TECHNICAL", "on")
    with pytest.raises(RuntimeError, match="requires the optional"):
        native_module.native_technical_metrics([1.0], [1.0])


def test_native_technical_facade_rejects_invalid_mode(monkeypatch) -> None:
    monkeypatch.setenv("ASHARE_NATIVE_TECHNICAL", "sometimes")
    with pytest.raises(ValueError, match="must be auto, on, or off"):
        native_module.native_technical_metrics([1.0], [1.0])
