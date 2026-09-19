"""
回测对比报告生成器。

生成 Legacy vs Jev 决策模式的对比报告：
- 性能指标对比
- 信号差异分析
- 决策分布对比
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from typing import NamedTuple

from pydantic import BaseModel

from ashare_ai.backtest.engine import BacktestResult

logger = logging.getLogger(__name__)


class PerformanceMetrics(NamedTuple):
    """性能指标"""

    total_return: float
    annualized_return: float
    sharpe_ratio: float
    max_drawdown: float
    win_rate: float
    turnover_rate: float
    num_trades: int


class SignalDifference(BaseModel):
    """信号差异"""

    symbol: str
    trading_date: date
    legacy_weight: Decimal
    jev_weight: Decimal
    weight_diff: Decimal
    legacy_action: str | None = None
    jev_action: str | None = None


class ModeComparisonReport(BaseModel):
    """模式对比报告"""

    legacy_metrics: PerformanceMetrics
    jev_metrics: PerformanceMetrics
    signal_differences: list[SignalDifference]
    agreement_rate: float  # 信号一致性
    summary: str


class BacktestComparisonGenerator:
    """回测对比报告生成器"""

    def __init__(self):
        pass

    def generate_comparison_report(
        self,
        legacy_result: BacktestResult,
        jev_result: BacktestResult,
    ) -> ModeComparisonReport:
        """
        生成对比报告。

        Args:
            legacy_result: Legacy 模式回测结果
            jev_result: Jev 模式回测结果

        Returns:
            ModeComparisonReport: 对比报告
        """
        legacy_metrics = self._extract_metrics(legacy_result)
        jev_metrics = self._extract_metrics(jev_result)

        signal_differences = self._analyze_signal_differences(legacy_result, jev_result)

        agreement_rate = self._calculate_agreement_rate(signal_differences)

        summary = self._generate_summary(legacy_metrics, jev_metrics, agreement_rate)

        return ModeComparisonReport(
            legacy_metrics=legacy_metrics,
            jev_metrics=jev_metrics,
            signal_differences=signal_differences,
            agreement_rate=agreement_rate,
            summary=summary,
        )

    def _extract_metrics(self, result: BacktestResult) -> PerformanceMetrics:
        """从回测结果提取性能指标"""
        # TODO: 从 BacktestResult 提取指标
        return PerformanceMetrics(
            total_return=float(result.summary.total_return) if result.summary else 0.0,
            annualized_return=float(result.summary.annualized_return) if result.summary else 0.0,
            sharpe_ratio=float(result.summary.sharpe_ratio) if result.summary else 0.0,
            max_drawdown=float(result.summary.max_drawdown) if result.summary else 0.0,
            win_rate=0.0,  # TODO: 计算胜率
            turnover_rate=0.0,  # TODO: 计算换手率
            num_trades=len(result.executions),
        )

    def _analyze_signal_differences(
        self,
        legacy_result: BacktestResult,
        jev_result: BacktestResult,
    ) -> list[SignalDifference]:
        """分析信号差异"""
        differences: list[SignalDifference] = []

        # TODO: 逐日对比信号差异

        return differences

    def _calculate_agreement_rate(self, differences: list[SignalDifference]) -> float:
        """计算信号一致性"""
        if not differences:
            return 1.0

        # TODO: 计算一致性（权重差异 < 0.05 视为一致）
        return 0.0

    def _generate_summary(
        self,
        legacy_metrics: PerformanceMetrics,
        jev_metrics: PerformanceMetrics,
        agreement_rate: float,
    ) -> str:
        """生成文字总结"""
        return f"""
Legacy 模式:
- 年化收益: {legacy_metrics.annualized_return:.2%}
- 夏普比率: {legacy_metrics.sharpe_ratio:.2f}
- 最大回撤: {legacy_metrics.max_drawdown:.2%}
- 交易次数: {legacy_metrics.num_trades}

Jev 模式:
- 年化收益: {jev_metrics.annualized_return:.2%}
- 夏普比率: {jev_metrics.sharpe_ratio:.2f}
- 最大回撤: {jev_metrics.max_drawdown:.2%}
- 交易次数: {jev_metrics.num_trades}

信号一致性: {agreement_rate:.2%}

优势模式: {"Legacy" if legacy_metrics.sharpe_ratio > jev_metrics.sharpe_ratio else "Jev"}
""".strip()

    def export_report(self, report: ModeComparisonReport, output_path: str) -> None:
        """导出报告为 JSON"""
        import json
        from pathlib import Path

        Path(output_path).write_text(
            json.dumps(report.model_dump(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        logger.info(f"Comparison report exported to {output_path}")
