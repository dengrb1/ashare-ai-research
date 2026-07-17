from __future__ import annotations

from math import sqrt

import numpy as np
from pydantic import BaseModel, ConfigDict


class PerformanceMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    total_return: float
    annualized_return: float
    annualized_volatility: float
    sharpe_ratio: float
    maximum_drawdown: float
    turnover: float


def calculate_performance_metrics(
    nav_values: list[float] | tuple[float, ...],
    *,
    annualization_sessions: int,
    annual_risk_free_rate: float,
    turnover: float,
) -> PerformanceMetrics:
    if annualization_sessions <= 0:
        raise ValueError("annualization_sessions must be positive")
    if not nav_values or any(value <= 0 for value in nav_values):
        raise ValueError("NAV values must be positive")
    nav = np.asarray(nav_values, dtype=float)
    returns = nav[1:] / nav[:-1] - 1.0
    total_return = float(nav[-1] / nav[0] - 1.0)
    if len(returns) == 0:
        annualized_return = 0.0
        volatility = 0.0
        sharpe = 0.0
    else:
        annualized_return = float(
            (1.0 + total_return) ** (annualization_sessions / len(returns)) - 1.0
        )
        volatility = float(np.std(returns, ddof=0) * sqrt(annualization_sessions))
        daily_risk_free = (1.0 + annual_risk_free_rate) ** (1.0 / annualization_sessions) - 1.0
        daily_excess = returns - daily_risk_free
        daily_std = float(np.std(returns, ddof=0))
        sharpe = (
            float(np.mean(daily_excess) / daily_std * sqrt(annualization_sessions))
            if daily_std > 0
            else 0.0
        )
    running_peak = np.maximum.accumulate(nav)
    drawdowns = 1.0 - nav / running_peak
    return PerformanceMetrics(
        total_return=total_return,
        annualized_return=annualized_return,
        annualized_volatility=volatility,
        sharpe_ratio=sharpe,
        maximum_drawdown=float(np.max(drawdowns)),
        turnover=turnover,
    )
