from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from ashare_ai.backtest.engine import (
    BacktestConfig,
    BacktestSignal,
    BenchmarkSeriesInput,
    FrozenInputManifest,
    PointInTimeStatistic,
)
from ashare_ai.orchestration.builtin_backtest import (
    BacktestBundle,
    BuiltinBacktestExecutor,
    TradingRuleObservation,
    write_backtest_bundle,
)
from ashare_ai.trading.execution import ExecutionBar, ExecutionConfig, SlippageTier
from ashare_ai.trading.rules import PriceLimitPolicy, TradingRule

SHANGHAI = ZoneInfo("Asia/Shanghai")
BAR_HASH = "a" * 64
RULE_HASH = "b" * 64
STAT_HASH = "c" * 64
SIGNAL_HASH = "d" * 64
BENCHMARK_HASH = "e" * 64
BENCHMARKS = ("CSI300", "CSI500", "EQUAL_WEIGHT_POOL")


def test_builtin_executor_is_reproducible_and_input_sensitive(tmp_path: Path) -> None:
    bundle = _bundle()
    uri, digest = write_backtest_bundle(tmp_path / "bundle.parquet", bundle)
    executor = BuiltinBacktestExecutor()
    job_config = {
        "artifact_root": str(tmp_path / "artifacts"),
        "snapshot_file_hashes": {"snapshot-1": digest},
    }
    first = executor.execute(
        backtest_id="fixed",
        config=job_config,
        snapshot_uris={"snapshot-1": uri},
    )
    second = executor.execute(
        backtest_id="fixed",
        config=job_config,
        snapshot_uris={"snapshot-1": uri},
    )
    assert first.output_hash == second.output_hash
    assert first.metrics["input_hash"] == second.metrics["input_hash"]
    assert first.artifacts == second.artifacts
    assert len(first.metrics["benchmarks"]) == 3
    assert first.metrics["capacity"]
    assert first.metrics["symbol_attribution"]
    for artifact in first.artifacts.values():
        assert _uri_path(artifact["uri"]).is_file()
        assert artifact["sha256"] == _sha256(_uri_path(artifact["uri"]))

    changed_adv = list(bundle.adv_amounts)
    changed_adv[0] = changed_adv[0].model_copy(update={"value": Decimal("90000000")})
    changed = bundle.model_copy(update={"adv_amounts": tuple(changed_adv)})
    changed_uri, changed_digest = write_backtest_bundle(tmp_path / "changed.parquet", changed)
    changed_output = executor.execute(
        backtest_id="changed",
        config={
            "artifact_root": str(tmp_path / "artifacts"),
            "snapshot_file_hashes": {"snapshot-2": changed_digest},
        },
        snapshot_uris={"snapshot-2": changed_uri},
    )
    assert changed_output.metrics["input_hash"] != first.metrics["input_hash"]
    assert changed_output.output_hash != first.output_hash


def test_builtin_executor_rejects_hash_schema_non_raw_and_future_data(
    tmp_path: Path,
) -> None:
    bundle = _bundle()
    uri, _digest = write_backtest_bundle(tmp_path / "bundle.parquet", bundle)
    executor = BuiltinBacktestExecutor()
    with pytest.raises(ValueError, match="snapshot hash mismatch"):
        executor.execute(
            backtest_id="bad-hash",
            config={
                "artifact_root": str(tmp_path / "artifacts"),
                "snapshot_file_hashes": {"snapshot-1": "0" * 64},
            },
            snapshot_uris={"snapshot-1": uri},
        )
    missing_path = tmp_path / "missing-column.parquet"
    missing_table = pa.Table.from_pylist([{"kind": "BACKTEST_CONFIG"}]).replace_schema_metadata(
        {b"bundle_schema_version": b"1", b"snapshot_status": b"COMMITTED"}
    )
    pq.write_table(missing_table, missing_path)
    with pytest.raises(ValueError, match="missing columns"):
        executor.execute(
            backtest_id="missing",
            config={
                "artifact_root": str(tmp_path / "artifacts"),
                "snapshot_file_hashes": {"snapshot-2": _sha256(missing_path)},
            },
            snapshot_uris={"snapshot-2": missing_path.resolve().as_uri()},
        )

    table = pq.read_table(_uri_path(uri))
    rows = table.to_pylist()
    for row in rows:
        if row["kind"] == "EXECUTION_BAR":
            payload = json.loads(row["payload_json"])
            payload["price_basis"] = "ADJUSTED"
            row["payload_json"] = json.dumps(payload)
            break
    non_raw_path = tmp_path / "non-raw.parquet"
    non_raw_table = pa.Table.from_pylist(rows).replace_schema_metadata(table.schema.metadata)
    pq.write_table(non_raw_table, non_raw_path)
    with pytest.raises(ValueError, match="price_basis"):
        executor.execute(
            backtest_id="non-raw",
            config={
                "artifact_root": str(tmp_path / "artifacts"),
                "snapshot_file_hashes": {"snapshot-3": _sha256(non_raw_path)},
            },
            snapshot_uris={"snapshot-3": non_raw_path.resolve().as_uri()},
        )

    future_bars = list(bundle.bars)
    future_bars[0] = future_bars[0].model_copy(
        update={"available_at": bundle.signals[0].decision_at + timedelta(seconds=1)}
    )
    future_bundle = bundle.model_copy(update={"bars": tuple(future_bars)})
    future_uri, future_digest = write_backtest_bundle(tmp_path / "future.parquet", future_bundle)
    with pytest.raises(ValueError, match="future bar"):
        executor.execute(
            backtest_id="future",
            config={
                "artifact_root": str(tmp_path / "artifacts"),
                "snapshot_file_hashes": {"snapshot-4": future_digest},
            },
            snapshot_uris={"snapshot-4": future_uri},
        )


def test_builtin_executor_records_industry_change_warning_and_attribution(tmp_path: Path) -> None:
    bundle = _bundle()
    changed_signals = list(bundle.signals)
    changed_signals[1] = changed_signals[1].model_copy(
        update={"industry_code": "PLACEHOLDER_3"}
    )
    changed = bundle.model_copy(update={"signals": tuple(changed_signals)})
    uri, digest = write_backtest_bundle(tmp_path / "industry-change.parquet", changed)
    output = BuiltinBacktestExecutor().execute(
        backtest_id="industry-change",
        config={
            "artifact_root": str(tmp_path / "artifacts"),
            "snapshot_file_hashes": {"snapshot-1": digest},
        },
        snapshot_uris={"snapshot-1": uri},
    )
    assert "BANK -> PLACEHOLDER_3" in output.metrics["warnings"][0]
    assert output.metrics["industry_classification_changes"][0]["symbol"] == "600000.SH"
    attribution = json.loads(
        _uri_path(output.artifacts["attribution"]["uri"]).read_text(encoding="utf-8")
    )
    assert attribution["attribution_method"] == (
        "latest-research-industry-at-or-before-terminal-exposure-date"
    )
    assert attribution["industry_classification_changes"][0]["new_industry_code"] == (
        "PLACEHOLDER_3"
    )


def _bundle() -> BacktestBundle:
    days = (date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6))
    frozen_at = datetime(2025, 1, 6, 20, tzinfo=SHANGHAI)
    engine_config = BacktestConfig(
        initial_cash=Decimal("10000"),
        annualization_sessions=242,
        annual_risk_free_rate=0.0,
        execution=ExecutionConfig(
            participation_rate=Decimal("1"),
            price_limit_policy=PriceLimitPolicy(
                require_official_limits=True,
                default_price_tick=Decimal("0.01"),
            ),
            slippage_tiers=(
                SlippageTier(
                    minimum_adv_amount=Decimal("0"),
                    half_spread_bps=Decimal("0"),
                ),
            ),
            impact_coefficient=Decimal("0"),
            max_slippage_bps=Decimal("0"),
            fee_quantum=Decimal("0.01"),
            odd_lot_sell_all_only=True,
        ),
        config_version="v1",
        input_manifest=FrozenInputManifest(
            manifest_id="bundle-fixture",
            frozen_at=frozen_at,
            snapshot_hashes=(
                BAR_HASH,
                RULE_HASH,
                STAT_HASH,
                SIGNAL_HASH,
                BENCHMARK_HASH,
            ),
        ),
        required_benchmarks=BENCHMARKS,
        capacity_scale_factors=(Decimal("1"), Decimal("10")),
        capacity_max_participation=Decimal("0.05"),
        capacity_max_slippage_bps=Decimal("50"),
        capacity_min_fill_rate=Decimal("0.95"),
    )
    bars = tuple(
        _bar(day, opening, closing, previous)
        for day, opening, closing, previous in (
            (days[0], "10", "10", "10"),
            (days[1], "10", "11", "10"),
            (days[2], "11", "11", "11"),
        )
    )
    rules = tuple(
        TradingRuleObservation(
            trading_date=day,
            symbol="600000.SH",
            rule=_rule(day),
        )
        for day in days
    )
    signals = (
        _signal(days[0], "0.5"),
        _signal(days[1], "0"),
    )
    adv = tuple(_stat(day, "100000000") for day in days)
    volatility = tuple(_stat(day, "0") for day in days)
    benchmarks = tuple(
        BenchmarkSeriesInput(
            name=name,
            available_at=frozen_at,
            snapshot_hash=BENCHMARK_HASH,
            returns={days[0]: 0.0, days[1]: 0.01, days[2]: -0.01},
        )
        for name in BENCHMARKS
    )
    return BacktestBundle(
        config=engine_config,
        trading_calendar=days,
        bars=bars,
        rules=rules,
        signals=signals,
        adv_amounts=adv,
        volatilities=volatility,
        benchmarks=benchmarks,
    )


def _bar(day: date, opening: str, closing: str, previous: str) -> ExecutionBar:
    open_price = Decimal(opening)
    close_price = Decimal(closing)
    prev_close = Decimal(previous)
    return ExecutionBar(
        symbol="600000.SH",
        trading_date=day,
        available_at=datetime(day.year, day.month, day.day, 17, tzinfo=SHANGHAI),
        snapshot_hash=BAR_HASH,
        open=open_price,
        high=max(open_price, close_price),
        low=min(open_price, close_price),
        close=close_price,
        volume=100_000,
        amount=Decimal("1000000"),
        prev_close=prev_close,
        official_limit_up=prev_close * Decimal("1.1"),
        official_limit_down=prev_close * Decimal("0.9"),
    )


def _rule(day: date) -> TradingRule:
    available_at = datetime(2025, 1, 1, 12, tzinfo=SHANGHAI)
    return TradingRule(
        rule_id=f"rule-{day}",
        rule_version="v1",
        priority=1,
        exchange="SH",
        market="A",
        board="MAIN",
        security_type="STOCK",
        risk_status="NORMAL",
        is_st=False,
        effective_from=day,
        published_at=available_at,
        available_at=available_at,
        source_snapshot_hash=RULE_HASH,
        price_limit_ratio=Decimal("0.10"),
        lot_size=100,
        stamp_tax_rate=Decimal("0"),
        commission_rate=Decimal("0"),
        minimum_commission=Decimal("0"),
        transfer_fee_rate=Decimal("0"),
        details={"price_tick": "0.01"},
    )


def _signal(day: date, weight: str) -> BacktestSignal:
    return BacktestSignal(
        signal_date=day,
        decision_at=datetime(day.year, day.month, day.day, 18, tzinfo=SHANGHAI),
        snapshot_hash=SIGNAL_HASH,
        symbol="600000.SH",
        industry_code="BANK",
        target_weight=Decimal(weight),
    )


def _stat(day: date, value: str) -> PointInTimeStatistic:
    return PointInTimeStatistic(
        symbol="600000.SH",
        trading_date=day,
        available_at=datetime.combine(
            day - timedelta(days=1),
            datetime.min.time().replace(hour=18),
            tzinfo=SHANGHAI,
        ),
        snapshot_hash=STAT_HASH,
        value=Decimal(value),
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _uri_path(uri: str) -> Path:
    parsed = urlparse(uri)
    raw = unquote(parsed.path)
    if len(raw) >= 3 and raw[0] == "/" and raw[2] == ":":
        raw = raw[1:]
    return Path(raw)
