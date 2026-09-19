"""
回测对比 CLI 命令。

提供命令行接口用于：
- 运行 Legacy vs Jev 回测对比
- 生成对比报告
- 导出结果
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from pathlib import Path

import typer

from ashare_ai.backtest.comparison_report import BacktestComparisonGenerator

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="backtest-compare",
    help="决策模式回测对比命令",
    no_args_is_help=True,
)


@app.command(name="compare")
def compare_modes(
    bundle_dir: Path = typer.Option(..., "--bundle-dir", help="Bundle 目录"),
    start_date: str = typer.Option(..., "--start-date", help="回测起始日期 (YYYY-MM-DD)"),
    end_date: str = typer.Option(..., "--end-date", help="回测结束日期"),
    initial_cash: float = typer.Option(1000000.0, "--initial-cash", help="初始资金"),
    output_dir: Path = typer.Option(
        Path("backtest_comparison"),
        "--output-dir",
        help="报告输出目录",
    ),
) -> None:
    """
    对比 Legacy 和 Jev 模式的回测结果。

    运行两个独立的回测：
    1. Legacy 模式
    2. Jev 模式

    生成对比报告。
    """
    typer.echo("Running backtest comparison...")
    typer.echo(f"  Period: {start_date} to {end_date}")
    typer.echo(f"  Initial cash: {initial_cash:,.0f}")

    output_dir.mkdir(parents=True, exist_ok=True)

    # TODO: 加载 Bundle
    # TODO: 运行 Legacy 回测
    # TODO: 运行 Jev 回测
    # TODO: 生成对比报告

    typer.echo("\nComparison not yet fully implemented")
    typer.echo(f"Reports will be saved to: {output_dir}")


@app.command(name="analyze-signals")
def analyze_signal_differences(
    legacy_result: Path = typer.Option(..., "--legacy-result", help="Legacy 回测结果文件"),
    jev_result: Path = typer.Option(..., "--jev-result", help="Jev 回测结果文件"),
    output_path: Path = typer.Option(
        Path("signal_analysis.json"),
        "--output",
        help="分析结果输出路径",
    ),
) -> None:
    """分析两个模式的信号差异"""
    typer.echo("Analyzing signal differences...")
    typer.echo(f"  Legacy: {legacy_result}")
    typer.echo(f"  Jev: {jev_result}")

    # TODO: 加载回测结果
    # TODO: 分析信号差异
    # TODO: 导出分析报告

    typer.echo(f"\nAnalysis will be saved to: {output_path}")


@app.command(name="generate-report")
def generate_report_from_results(
    legacy_result: Path = typer.Option(..., "--legacy-result", help="Legacy 回测结果"),
    jev_result: Path = typer.Option(..., "--jev-result", help="Jev 回测结果"),
    output_path: Path = typer.Option(
        Path("comparison_report.json"),
        "--output",
        help="报告输出路径",
    ),
) -> None:
    """从已有的回测结果生成对比报告"""
    if not legacy_result.exists():
        typer.echo(f"Error: Legacy result not found: {legacy_result}")
        raise typer.Exit(1)

    if not jev_result.exists():
        typer.echo(f"Error: Jev result not found: {jev_result}")
        raise typer.Exit(1)

    typer.echo("Generating comparison report...")

    # TODO: 加载回测结果
    # TODO: 调用 BacktestComparisonGenerator
    # TODO: 导出报告

    typer.echo(f"Report generated: {output_path}")


if __name__ == "__main__":
    app()
