from __future__ import annotations

# The benchmark deliberately selects either the working tree or a detached
# comparison tree before importing the package under measurement.
# ruff: noqa: E402
import argparse
import gc
import json
import os
import runpy
import statistics
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import psutil

ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = Path(os.environ.get("ASHARE_BENCH_SOURCE_ROOT", ROOT))
sys.path.insert(0, str(SOURCE_ROOT / "src"))

from ashare_ai.backtest.engine import BacktestEngine, BenchmarkSeriesInput
from ashare_ai.backtest.trade_plan import optimize_trade_strategy
from ashare_ai.core.contracts import SnapshotStatus
from ashare_ai.core.hashing import stable_hash
from ashare_ai.storage.lake import ImmutableLake


def _measure(operation: Callable[[], str], iterations: int) -> dict[str, Any]:
    samples: list[float] = []
    peaks: list[int] = []
    hashes: list[str] = []
    process = psutil.Process()
    for _ in range(iterations):
        gc.collect()
        peak = process.memory_info().rss
        stop = threading.Event()

        def sample_rss(sample_stop: threading.Event = stop) -> None:
            nonlocal peak
            while not sample_stop.wait(0.005):
                peak = max(peak, process.memory_info().rss)

        sampler = threading.Thread(target=sample_rss, daemon=True)
        sampler.start()
        started = time.perf_counter()
        hashes.append(operation())
        samples.append((time.perf_counter() - started) * 1000)
        stop.set()
        sampler.join()
        peaks.append(peak)
    if len(set(hashes)) != 1:
        raise RuntimeError(f"benchmark output is not deterministic: {hashes}")
    return {
        "iterations": iterations,
        "duration_ms": samples,
        "median_duration_ms": statistics.median(samples),
        "peak_rss_bytes": peaks,
        "median_peak_rss_bytes": int(statistics.median(peaks)),
        "output_hash": hashes[0],
    }


def _trade_plan_operation() -> Callable[[], str]:
    fixture = runpy.run_path(str(ROOT / "tests/unit/test_trade_plan_optimizer.py"))
    calendar, bars = fixture["_history"](240)
    rules = {day: fixture["_rule"](day) for day in calendar}
    adv = {day: Decimal("100000000") for day in calendar}
    volatility = {day: 0.2 for day in calendar}
    config = fixture["_config"]()

    def run() -> str:
        result = optimize_trade_strategy(
            symbol="600000.SH",
            trading_calendar=calendar,
            bars=bars,
            rules=rules,
            adv_amounts=adv,
            volatilities=volatility,
            execution_config=config,
        )
        return stable_hash(result)

    return run


def _backtest_operation(symbol_count: int = 200, session_count: int = 242) -> Callable[[], str]:
    fixture = runpy.run_path(str(ROOT / "tests/unit/test_backtest.py"))
    base_engine, _, _, _, _, _, _ = fixture["make_fixture"]()
    calendar: list[date] = []
    current = date(2025, 1, 2)
    while len(calendar) < session_count:
        if current.weekday() < 5:
            calendar.append(current)
        current += timedelta(days=1)
    symbols = tuple(f"{600000 + index:06d}.SH" for index in range(symbol_count))
    bars = {}
    rules = {}
    adv = {}
    volatility = {}
    for value_date in calendar:
        base_bar = fixture["make_bar"](
            value_date, open_price="10", close_price="10.01", prev_close="10"
        )
        base_rule = fixture["make_rule"](value_date)
        visible = datetime.combine(
            value_date - timedelta(days=1),
            datetime.min.time().replace(hour=18),
            tzinfo=fixture["SHANGHAI"],
        )
        for symbol in symbols:
            key = (value_date, symbol)
            bars[key] = base_bar.model_copy(update={"symbol": symbol})
            rules[key] = base_rule
            statistic = fixture["PointInTimeStatistic"](
                symbol=symbol,
                trading_date=value_date,
                available_at=visible,
                snapshot_hash=fixture["STAT_HASH"],
                value=Decimal("100000000"),
            )
            adv[key] = statistic
            volatility[key] = statistic.model_copy(update={"value": Decimal("0.2")})
    frozen_at = datetime.combine(
        calendar[-1], datetime.min.time().replace(hour=20), tzinfo=fixture["SHANGHAI"]
    )
    benchmark_returns = {day: 0.0 for day in calendar}
    benchmarks = tuple(
        BenchmarkSeriesInput(
            name=name,
            available_at=frozen_at,
            snapshot_hash=fixture["BENCHMARK_HASH"],
            returns=benchmark_returns,
        )
        for name in fixture["BENCHMARKS"]
    )
    engine = BacktestEngine(
        base_engine.config.model_copy(
            update={
                "input_manifest": base_engine.config.input_manifest.model_copy(
                    update={"frozen_at": frozen_at}
                )
            }
        )
    )

    def run() -> str:
        return engine.run(
            trading_calendar=calendar,
            bars=bars,
            rules=rules,
            signals=(),
            adv_amounts=adv,
            volatilities=volatility,
            benchmark_series=benchmarks,
        ).output_hash

    return run


def _lake_operation(
    row_count: int = 200_000,
) -> tuple[Callable[[], str], tempfile.TemporaryDirectory[str]]:
    temporary = tempfile.TemporaryDirectory()
    lake = ImmutableLake(temporary.name)
    manifest = lake.write_snapshot(
        dataset="benchmark_rows",
        source="fixed-generator-v1",
        schema_version="1",
        adapter_version="1",
        fetched_at=datetime(2026, 8, 8, 18, tzinfo=UTC),
        rows=({"value": index, "group_id": index % 100} for index in range(row_count)),
    ).model_copy(update={"status": SnapshotStatus.COMMITTED})

    def run() -> str:
        rows = lake.query(
            "SELECT group_id, sum(value) AS total "
            "FROM snapshot GROUP BY group_id ORDER BY group_id",
            [manifest],
        )
        return stable_hash(rows)

    return run, temporary


def main() -> None:
    parser = argparse.ArgumentParser(description="Fixed-input hotspot benchmarks")
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--suite", choices=("all", "trade-plan", "backtest", "lake"), default="all")
    args = parser.parse_args()
    if args.iterations < 3:
        parser.error("--iterations must be at least 3")
    output: dict[str, Any] = {}
    temporary: tempfile.TemporaryDirectory[str] | None = None
    if args.suite in ("all", "trade-plan"):
        output["trade_plan"] = _measure(_trade_plan_operation(), args.iterations)
    if args.suite in ("all", "backtest"):
        output["backtest"] = _measure(_backtest_operation(), args.iterations)
    if args.suite in ("all", "lake"):
        operation, temporary = _lake_operation()
        output["lake_query"] = _measure(operation, args.iterations)
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    if temporary is not None:
        temporary.cleanup()


if __name__ == "__main__":
    main()
