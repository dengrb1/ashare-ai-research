"""PIT validated, deterministic monitoring signals."""

from __future__ import annotations

import statistics
from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from ashare_ai.features.native import native_monitor_signals

SIGNAL_VERSION = "monitor-signals-v1"
SignalType = Literal[
    "INTRADAY_DROP", "VOLUME_BREAKOUT", "VOLUME_PRICE_DIVERGENCE", "MA_DEATH_CROSS"
]


class MonitorSignal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    trading_date: date
    available_at: AwareDatetime
    decision_at: AwareDatetime
    signal_type: SignalType
    severity: Literal["LOW", "MEDIUM", "HIGH"]
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: dict[str, float | int | str | bool]
    threshold_version: str = SIGNAL_VERSION


def detect_monitor_signals(
    *,
    symbol: str,
    bars: list[dict[str, Any]],
    available_at: datetime,
    decision_at: datetime,
    thresholds: dict[str, float] | None = None,
) -> list[MonitorSignal]:
    """Evaluate ordered bars after enforcing the PIT boundary.

    A signal is emitted only from bars available no later than ``decision_at``;
    the caller supplies the provider collection timestamp as ``available_at``.
    """
    if available_at.tzinfo is None or decision_at.tzinfo is None:
        raise ValueError("available_at and decision_at must be timezone-aware")
    available_at = available_at.astimezone(UTC)
    decision_at = decision_at.astimezone(UTC)
    if available_at > decision_at:
        raise ValueError("available_at must not be after decision_at")
    visible = []
    for bar in bars:
        timestamp = bar.get("timestamp", bar.get("time"))
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if not isinstance(timestamp, datetime) or timestamp.tzinfo is None:
            continue
        timestamp = timestamp.astimezone(UTC)
        if timestamp > decision_at:
            continue
        try:
            close = float(bar["close"])
            volume = float(bar.get("volume", 0.0))
        except (KeyError, TypeError, ValueError):
            continue
        if close <= 0 or volume < 0:
            continue
        visible.append((timestamp, close, volume))
    visible.sort(key=lambda item: item[0])
    if not visible:
        return []
    closes = [item[1] for item in visible]
    volumes = [item[2] for item in visible]
    native = native_monitor_signals(closes, volumes)
    if native is None:
        native = _python_signals(closes, volumes)
    latest = visible[-1][0]
    trading_date = latest.date()
    thresholds = thresholds or {}
    emitted: list[MonitorSignal] = []
    for signal_type, triggered, confidence, primary, secondary in native:
        if not triggered:
            continue
        confidence = max(0.0, min(1.0, float(confidence)))
        severity = "HIGH" if confidence >= 0.8 else "MEDIUM" if confidence >= 0.5 else "LOW"
        evidence = {
            "primary": round(float(primary), 8),
            "secondary": round(float(secondary), 8),
            "observations": len(visible),
            "source_timestamp": latest.isoformat(),
        }
        if signal_type == "INTRADAY_DROP":
            evidence["drop_threshold"] = thresholds.get("intraday_drop", -0.03)
        elif signal_type == "VOLUME_BREAKOUT":
            evidence["volume_threshold"] = thresholds.get("breakout_volume_ratio", 1.5)
        emitted.append(
            MonitorSignal(
                symbol=symbol.upper(),
                trading_date=trading_date,
                available_at=available_at,
                decision_at=decision_at,
                signal_type=signal_type,
                severity=severity,
                confidence=confidence,
                evidence=evidence,
            )
        )
    return emitted


def _python_signals(
    closes: list[float], volumes: list[float]
) -> list[tuple[str, bool, float, float, float]]:
    def item(
        name: str, triggered: bool, confidence: float, primary: float, secondary: float
    ):
        return name, triggered, confidence, primary, secondary

    drop = closes[-1] / closes[-4] - 1 if len(closes) >= 4 else 0.0
    price_ratio = 0.0
    volume_ratio = 0.0
    breakout = False
    if len(closes) >= 21:
        prior = closes[-21:-1]
        price_ratio = closes[-1] / max(prior) - 1
        average_volume = statistics.fmean(volumes[-21:-1])
        volume_ratio = volumes[-1] / average_volume if average_volume else 0.0
        breakout = price_ratio >= 0.01 and volume_ratio >= 1.5
    price_change = closes[-1] / closes[-6] - 1 if len(closes) >= 10 else 0.0
    first_volume = statistics.fmean(volumes[-10:-5]) if len(volumes) >= 10 else 0.0
    second_volume = statistics.fmean(volumes[-5:]) if len(volumes) >= 10 else 0.0
    volume_change = second_volume / first_volume - 1 if first_volume else 0.0
    death = False
    current_gap = previous_gap = 0.0
    if len(closes) >= 21:
        previous_gap = statistics.fmean(closes[-6:-1]) - statistics.fmean(closes[-21:-1])
        current_gap = statistics.fmean(closes[-5:]) - statistics.fmean(closes[-20:])
        death = previous_gap >= 0 and current_gap < 0
    return [
        item("INTRADAY_DROP", drop <= -0.03, min(1.0, max(0.0, -drop / 0.03)), drop, 4),
        item(
            "VOLUME_BREAKOUT",
            breakout,
            min(1.0, max(0.0, (price_ratio / 0.01 + (volume_ratio - 1.5) / 1.5) / 2)),
            price_ratio,
            volume_ratio,
        ),
        item(
            "VOLUME_PRICE_DIVERGENCE",
            price_change >= 0.03 and volume_change <= -0.20,
            min(
                1.0,
                max(0.0, abs(price_change) * max(0.0, -volume_change) / 0.03),
            ),
            price_change,
            volume_change,
        ),
        item(
            "MA_DEATH_CROSS",
            death,
            min(1.0, max(0.0, -current_gap / max(closes[-1], 1e-9) / 0.02)),
            current_gap / max(closes[-1], 1e-9),
            previous_gap / max(closes[-1], 1e-9),
        ),
    ]
