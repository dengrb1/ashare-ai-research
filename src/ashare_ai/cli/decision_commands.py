"""
CLI 命令：决策预测和模型管理。

提供命令行接口用于：
- 单个股票决策预测
- 批量决策预测
- 模型版本管理
- 决策模式切换
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from ashare_ai.core.config import load_config
from ashare_ai.orchestration.decision_integration import (
    batch_generate_decisions,
    generate_decision_for_symbol,
)

logger = logging.getLogger(__name__)
console = Console()

app = typer.Typer(
    name="decision",
    help="Decision prediction and model management commands",
    no_args_is_help=True,
)


@app.command(name="predict")
def predict_command(
    symbol: Annotated[str, typer.Argument(help="Stock symbol (e.g. 600000.SH)")],
    trading_date: Annotated[
        str | None,
        typer.Option("--date", "-d", help="Trading date (YYYY-MM-DD), default: latest"),
    ] = None,
    mode: Annotated[
        str,
        typer.Option("--mode", "-m", help="Decision mode: legacy or jev"),
    ] = "legacy",
    output: Annotated[
        str | None,
        typer.Option("--output", "-o", help="Output file path (JSON)"),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Verbose output")] = False,
) -> None:
    """
    Generate decision prediction for a single stock.

    Example:
        ashare-ai decision predict 600000.SH --mode legacy
        ashare-ai decision predict 000001.SZ --date 2026-07-17 --mode jev
    """
    if verbose:
        logging.basicConfig(level=logging.DEBUG)

    try:
        config = load_config()
        # 加载 Bundle - 使用配置的路径
        from ashare_ai.orchestration.bundle_loader import find_latest_bundle, load_bundle_from_disk

        bundle_dir = config.bundle_storage_dir

        if trading_date is None:
            date_obj = find_latest_bundle(bundle_dir)
            if date_obj is None:
                console.print(f"[red]错误: 在 {bundle_dir} 中未找到任何 Bundle[/red]")
                raise typer.Exit(1)
        else:
            from datetime import date
            date_obj = date.fromisoformat(trading_date)

        console.print(f"[cyan]加载 Bundle: {date_obj}[/cyan]")
        bundle = load_bundle_from_disk(bundle_dir, date_obj)

        # 生成决策
        import asyncio
        decision = asyncio.run(
            generate_decision_for_symbol(
                bundle=bundle,
                symbol=symbol,
                trading_date=date_obj,
                mode=mode,
            )
        )

        # 输出结果
        console.print(f"\n[green]决策结果 ({symbol}):[/green]")
        console.print(f"  动作: {decision.action}")
        console.print(f"  风险等级: {decision.risk_level}")
        console.print(f"  建议仓位: {decision.position_size:.2%}")
        console.print(f"  置信度: {decision.confidence:.2%}")
        console.print(f"  预测方向 (1天): {decision.probabilities.direction_1d.value}")
        console.print(f"  预测方向 (5天): {decision.probabilities.direction_5d.value}")
        console.print(f"  模型版本: {decision.model_version}")

        if output:
            import json
            output_path = Path(output)
            output_path.write_text(
                decision.model_dump_json(indent=2),
                encoding="utf-8",
            )
            console.print(f"\n[green]结果已保存到: {output}[/green]")

        console.print("[yellow]Warning: Bundle loading not yet integrated[/yellow]")
        console.print(f"[red]Predict command skeleton - symbol={symbol}, mode={mode}[/red]")

        # 占位符输出
        result = {
            "symbol": symbol,
            "mode": mode,
            "status": "not_implemented",
            "message": "Bundle loading and orchestration integration pending",
        }

        if output:
            Path(output).write_text(json.dumps(result, indent=2, ensure_ascii=False))
            console.print(f"[green]Results saved to {output}[/green]")
        else:
            console.print_json(json.dumps(result, indent=2, ensure_ascii=False))

    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(code=1)


@app.command(name="batch")
def batch_predict_command(
    symbols_file: Annotated[Path, typer.Argument(help="File with stock symbols (one per line)")],
    output: Annotated[Path, typer.Argument(help="Output JSON file")],
    mode: Annotated[str, typer.Option("--mode", "-m", help="Decision mode")] = "legacy",
    date: Annotated[str | None, typer.Option("--date", "-d")] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """
    Batch generate decisions for multiple stocks.

    Example:
        ashare-ai decision batch symbols.txt decisions.json --mode legacy
    """
    if verbose:
        logging.basicConfig(level=logging.DEBUG)

    try:
        symbols = [line.strip() for line in symbols_file.read_text().splitlines() if line.strip()]
        console.print(f"[blue]Loaded {len(symbols)} symbols from {symbols_file}[/blue]")

        # TODO: 实际实现
        console.print("[yellow]Batch prediction not yet implemented[/yellow]")

        result = {
            "symbols_count": len(symbols),
            "mode": mode,
            "status": "not_implemented",
        }
        output.write_text(json.dumps(result, indent=2, ensure_ascii=False))
        console.print(f"[green]Placeholder results saved to {output}[/green]")

    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(code=1)


@app.command(name="info")
def info_command() -> None:
    """
    Display current decision configuration.

    Example:
        ashare-ai decision info
    """
    try:
        config = load_config()

        table = Table(title="Decision Configuration")
        table.add_column("Setting", style="cyan")
        table.add_column("Value", style="green")

        table.add_row("Decision Mode", config.decision_mode)
        table.add_row("Fallback Enabled", str(config.decision_fallback_enabled))
        table.add_row("Jev Model Dir", str(config.jev_model_dir))
        table.add_row("Jev Model Version", config.jev_model_version)
        table.add_row("Jev Device", config.jev_device)

        console.print(table)

    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(code=1)


@app.command(name="list-models")
def list_models_command() -> None:
    """
    List available Jev models.

    Example:
        ashare-ai decision list-models
    """
    try:
        config = load_config()
        model_dir = config.jev_model_dir

        if not model_dir.exists():
            console.print(f"[yellow]Model directory not found: {model_dir}[/yellow]")
            return

        models = [d for d in model_dir.iterdir() if d.is_dir() and (d / "config.json").exists()]

        if not models:
            console.print("[yellow]No trained models found[/yellow]")
            return

        table = Table(title="Available Jev Models")
        table.add_column("Model Version", style="cyan")
        table.add_column("Path", style="green")

        for model in models:
            table.add_row(model.name, str(model))

        console.print(table)

    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
