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
    configured = {
        "intraday_drop": -0.03,
        "breakout_price_pct": 0.01,
        "breakout_volume_ratio": 1.5,
        "divergence_price_pct": 0.03,
        "divergence_volume_ratio": -0.20,
    }
    configured.update({key: float(value) for key, value in (thresholds or {}).items() if key in configured})
    # The native ABI has no threshold argument.  Use it only for the default
    # policy; custom policies must be evaluated by the Python implementation.
    use_default_policy = configured == {
        "intraday_drop": -0.03,
        "breakout_price_pct": 0.01,
        "breakout_volume_ratio": 1.5,
        "divergence_price_pct": 0.03,
        "divergence_volume_ratio": -0.20,
    }
    native = native_monitor_signals(closes, volumes) if use_default_policy else None
    if native is None:
        native = _python_signals(closes, volumes, thresholds=configured)
    latest = visible[-1][0]
    trading_date = latest.date()
    thresholds = configured
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
    closes: list[float],
    volumes: list[float],
    *,
    thresholds: dict[str, float] | None = None,
) -> list[tuple[str, bool, float, float, float]]:
    thresholds = thresholds or {
        "intraday_drop": -0.03,
        "breakout_price_pct": 0.01,
        "breakout_volume_ratio": 1.5,
        "divergence_price_pct": 0.03,
        "divergence_volume_ratio": -0.20,
    }
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
        breakout = (
            price_ratio >= thresholds["breakout_price_pct"]
            and volume_ratio >= thresholds["breakout_volume_ratio"]
        )
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
        item(
            "INTRADAY_DROP",
            drop <= thresholds["intraday_drop"],
            min(1.0, max(0.0, abs(drop) / max(abs(thresholds["intraday_drop"]), 1e-9))),
            drop,
            4,
        ),
        item(
            "VOLUME_BREAKOUT",
            breakout,
            min(
                1.0,
                max(
                    0.0,
                    (
                        price_ratio / max(thresholds["breakout_price_pct"], 1e-9)
                        + (volume_ratio - thresholds["breakout_volume_ratio"])
                        / max(thresholds["breakout_volume_ratio"], 1e-9)
                    )
                    / 2,
                ),
            ),
            price_ratio,
            volume_ratio,
        ),
        item(
            "VOLUME_PRICE_DIVERGENCE",
            price_change >= thresholds["divergence_price_pct"]
            and volume_change <= thresholds["divergence_volume_ratio"],
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
