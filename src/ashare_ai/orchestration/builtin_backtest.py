from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ashare_ai.backtest.engine import (
    BacktestConfig,
    BacktestEngine,
    BacktestSignal,
    BenchmarkSeriesInput,
    PointInTimeStatistic,
)
from ashare_ai.core.hashing import canonical_json
from ashare_ai.orchestration.backtest_jobs import BacktestJobExecutor, BacktestJobOutput
from ashare_ai.storage.objects import LocalObjectStore
from ashare_ai.trading.execution import ExecutionBar
from ashare_ai.trading.rules import TradingRule

BUNDLE_SCHEMA_VERSION = "1"


class BundleKind(StrEnum):
    BACKTEST_CONFIG = "BACKTEST_CONFIG"
    TRADING_CALENDAR = "TRADING_CALENDAR"
    EXECUTION_BAR = "EXECUTION_BAR"
    TRADING_RULE = "TRADING_RULE"
    BACKTEST_SIGNAL = "BACKTEST_SIGNAL"
    ADV_OBSERVATION = "ADV_OBSERVATION"
    VOLATILITY_OBSERVATION = "VOLATILITY_OBSERVATION"
    BENCHMARK_OBSERVATION = "BENCHMARK_OBSERVATION"


class TradingCalendarRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dates: tuple[date, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_dates(self) -> TradingCalendarRecord:
        if tuple(sorted(set(self.dates))) != self.dates:
            raise ValueError("trading calendar must be sorted and unique")
        return self


class TradingRuleObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    trading_date: date
    symbol: str
    rule: TradingRule


class BacktestBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    config: BacktestConfig
    trading_calendar: tuple[date, ...] = Field(min_length=1)
    bars: tuple[ExecutionBar, ...] = Field(min_length=1)
    rules: tuple[TradingRuleObservation, ...] = Field(min_length=1)
    signals: tuple[BacktestSignal, ...]
    adv_amounts: tuple[PointInTimeStatistic, ...] = Field(min_length=1)
    volatilities: tuple[PointInTimeStatistic, ...] = Field(min_length=1)
    benchmarks: tuple[BenchmarkSeriesInput, ...] = Field(min_length=3)

    @model_validator(mode="after")
    def validate_keys(self) -> BacktestBundle:
        if tuple(sorted(set(self.trading_calendar))) != self.trading_calendar:
            raise ValueError("trading calendar must be sorted and unique")
        _require_unique(
            ((item.trading_date, item.symbol) for item in self.bars),
            "execution bars",
        )
        _require_unique(
            ((item.trading_date, item.symbol) for item in self.rules),
            "trading rules",
        )
        _require_unique(
            ((item.trading_date, item.symbol) for item in self.adv_amounts),
            "ADV observations",
        )
        _require_unique(
            ((item.trading_date, item.symbol) for item in self.volatilities),
            "volatility observations",
        )
        _require_unique((item.name for item in self.benchmarks), "benchmarks")
        return self


class BuiltinExecutorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_root: Path = Path("data/artifacts")
    snapshot_file_hashes: dict[str, str] = Field(min_length=1)
    requested_start_date: date | None = None
    requested_end_date: date | None = None
    initial_capital: Decimal | None = Field(default=None, gt=0)
    benchmark: str | None = None

    @model_validator(mode="after")
    def validate_hashes(self) -> BuiltinExecutorConfig:
        if any(len(value) != 64 for value in self.snapshot_file_hashes.values()):
            raise ValueError("snapshot_file_hashes must contain SHA-256 values")
        if (
            self.requested_start_date is not None
            and self.requested_end_date is not None
            and self.requested_start_date > self.requested_end_date
        ):
            raise ValueError("requested_start_date must be <= requested_end_date")
        return self


def write_backtest_bundle(path: str | Path, bundle: BacktestBundle) -> tuple[str, str]:
    """Write the canonical kind/payload_json fixed-snapshot bundle used by the executor."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, str]] = []

    def add(kind: BundleKind, payload: BaseModel | Mapping[str, Any]) -> None:
        value: Any = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
        rows.append(
            {
                "kind": kind.value,
                "payload_json": canonical_json(value).decode("utf-8"),
            }
        )

    add(BundleKind.BACKTEST_CONFIG, bundle.config)
    add(BundleKind.TRADING_CALENDAR, TradingCalendarRecord(dates=bundle.trading_calendar))
    for bar in sorted(bundle.bars, key=lambda value: (value.trading_date, value.symbol)):
        add(BundleKind.EXECUTION_BAR, bar)
    for rule_observation in sorted(
        bundle.rules, key=lambda value: (value.trading_date, value.symbol)
    ):
        add(BundleKind.TRADING_RULE, rule_observation)
    for signal in sorted(bundle.signals, key=lambda value: (value.signal_date, value.symbol)):
        add(BundleKind.BACKTEST_SIGNAL, signal)
    for adv in sorted(bundle.adv_amounts, key=lambda value: (value.trading_date, value.symbol)):
        add(BundleKind.ADV_OBSERVATION, adv)
    for volatility in sorted(
        bundle.volatilities, key=lambda value: (value.trading_date, value.symbol)
    ):
        add(BundleKind.VOLATILITY_OBSERVATION, volatility)
    for benchmark in sorted(bundle.benchmarks, key=lambda value: value.name):
        add(BundleKind.BENCHMARK_OBSERVATION, benchmark)

    table = pa.Table.from_pylist(rows).replace_schema_metadata(
        {
            b"bundle_schema_version": BUNDLE_SCHEMA_VERSION.encode(),
            b"snapshot_status": b"COMMITTED",
        }
    )
    pq.write_table(table, target, compression="zstd")
    digest = _file_sha256(target)
    return target.resolve().as_uri(), digest


def read_backtest_bundle(
    snapshot_uris: Mapping[str, str],
    expected_file_hashes: Mapping[str, str],
) -> BacktestBundle:
    if not snapshot_uris:
        raise ValueError("at least one committed snapshot URI is required")
    if set(snapshot_uris) != set(expected_file_hashes):
        raise ValueError("snapshot_file_hashes must exactly cover snapshot_uris")

    grouped: dict[BundleKind, list[dict[str, Any]]] = {kind: [] for kind in BundleKind}
    for snapshot_id, uri in sorted(snapshot_uris.items()):
        path = _local_path(uri)
        expected = expected_file_hashes[snapshot_id]
        actual = _file_sha256(path)
        if actual != expected:
            raise ValueError(
                f"snapshot hash mismatch for {snapshot_id}: expected={expected}, actual={actual}"
            )
        parquet = pq.ParquetFile(path)
        metadata = parquet.schema_arrow.metadata or {}
        if metadata.get(b"snapshot_status") != b"COMMITTED":
            raise ValueError(f"snapshot is not marked COMMITTED: {snapshot_id}")
        if metadata.get(b"bundle_schema_version") != BUNDLE_SCHEMA_VERSION.encode():
            raise ValueError(f"unsupported bundle schema for {snapshot_id}")
        table = parquet.read()
        required = {"kind", "payload_json"}
        missing = required - set(table.column_names)
        if missing:
            raise ValueError(f"snapshot {snapshot_id} is missing columns: {sorted(missing)}")
        for row_number, row in enumerate(table.select(sorted(required)).to_pylist()):
            raw_kind = row.get("kind")
            raw_payload = row.get("payload_json")
            if not isinstance(raw_kind, str) or not isinstance(raw_payload, str):
                raise ValueError(
                    f"snapshot {snapshot_id} row {row_number} has non-string bundle fields"
                )
            try:
                kind = BundleKind(raw_kind)
            except ValueError as exc:
                raise ValueError(f"unknown bundle kind: {raw_kind}") from exc
            try:
                payload = json.loads(raw_payload)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid payload_json in {snapshot_id} row {row_number}") from exc
            if not isinstance(payload, dict):
                raise ValueError("bundle payload_json must decode to an object")
            grouped[kind].append(payload)

    config_payload = _exactly_one(grouped[BundleKind.BACKTEST_CONFIG], "BACKTEST_CONFIG")
    calendar_payload = _exactly_one(grouped[BundleKind.TRADING_CALENDAR], "TRADING_CALENDAR")
    return BacktestBundle(
        config=BacktestConfig.model_validate(config_payload),
        trading_calendar=TradingCalendarRecord.model_validate(calendar_payload).dates,
        bars=tuple(ExecutionBar.model_validate(item) for item in grouped[BundleKind.EXECUTION_BAR]),
        rules=tuple(
            TradingRuleObservation.model_validate(item) for item in grouped[BundleKind.TRADING_RULE]
        ),
        signals=tuple(
            BacktestSignal.model_validate(item) for item in grouped[BundleKind.BACKTEST_SIGNAL]
        ),
        adv_amounts=tuple(
            PointInTimeStatistic.model_validate(item)
            for item in grouped[BundleKind.ADV_OBSERVATION]
        ),
        volatilities=tuple(
            PointInTimeStatistic.model_validate(item)
            for item in grouped[BundleKind.VOLATILITY_OBSERVATION]
        ),
        benchmarks=tuple(
            BenchmarkSeriesInput.model_validate(item)
            for item in grouped[BundleKind.BENCHMARK_OBSERVATION]
        ),
    )


class BuiltinBacktestExecutor:
    def execute(
        self,
        *,
        backtest_id: str,
        config: dict[str, Any],
        snapshot_uris: dict[str, str],
    ) -> BacktestJobOutput:
        del backtest_id
        executor_config = BuiltinExecutorConfig.model_validate(config)
        bundle = read_backtest_bundle(
            snapshot_uris,
            executor_config.snapshot_file_hashes,
        )
        selected = _select_requested_range(bundle, executor_config)
        result = BacktestEngine(selected.config).run(
            trading_calendar=selected.trading_calendar,
            bars={(item.trading_date, item.symbol): item for item in selected.bars},
            rules={(item.trading_date, item.symbol): item.rule for item in selected.rules},
            signals=selected.signals,
            adv_amounts={(item.trading_date, item.symbol): item for item in selected.adv_amounts},
            volatilities={(item.trading_date, item.symbol): item for item in selected.volatilities},
            benchmark_series=selected.benchmarks,
        )
        object_store = LocalObjectStore(executor_config.artifact_root)
        selected_benchmark_name = _benchmark_name(
            executor_config.benchmark or selected.config.required_benchmarks[0]
        )
        selected_benchmark = next(
            item for item in result.benchmarks if item.name == selected_benchmark_name
        )
        artifacts = {
            "nav": _put_json(
                object_store,
                [item.model_dump(mode="json") for item in result.daily_nav],
            ),
            "executions": _put_json(
                object_store,
                [item.model_dump(mode="json") for item in result.executions],
            ),
            "attribution": _put_json(
                object_store,
                {
                    "symbols": [item.model_dump(mode="json") for item in result.symbol_attribution],
                    "industries": [
                        item.model_dump(mode="json") for item in result.industry_attribution
                    ],
                    "costs": result.cost_attribution.model_dump(mode="json"),
                    "industry_classification_changes": [
                        item.model_dump(mode="json")
                        for item in result.industry_classification_changes
                    ],
                    "attribution_method": result.attribution_method,
                    "warnings": list(result.warnings),
                },
            ),
            "result": _put_json(object_store, result.model_dump(mode="json")),
        }
        return BacktestJobOutput(
            metrics={
                "performance": result.metrics.model_dump(mode="json"),
                "benchmarks": [item.model_dump(mode="json") for item in result.benchmarks],
                "selected_benchmark": selected_benchmark.model_dump(mode="json"),
                "capacity": [item.model_dump(mode="json") for item in result.capacity],
                "symbol_attribution": [
                    item.model_dump(mode="json") for item in result.symbol_attribution
                ],
                "industry_attribution": [
                    item.model_dump(mode="json") for item in result.industry_attribution
                ],
                "cost_attribution": result.cost_attribution.model_dump(mode="json"),
                "industry_classification_changes": [
                    item.model_dump(mode="json")
                    for item in result.industry_classification_changes
                ],
                "attribution_method": result.attribution_method,
                "warnings": list(result.warnings),
                "manifest_hash": result.manifest_hash,
                "input_hash": result.input_hash,
                "output_hash": result.output_hash,
            },
            artifacts=artifacts,
            output_hash=result.output_hash,
        )


def create_executor() -> BacktestJobExecutor:
    return BuiltinBacktestExecutor()


def _select_requested_range(
    bundle: BacktestBundle,
    config: BuiltinExecutorConfig,
) -> BacktestBundle:
    requested_start = config.requested_start_date or bundle.trading_calendar[0]
    requested_end = config.requested_end_date or bundle.trading_calendar[-1]
    initial_capital = config.initial_capital or bundle.config.initial_cash
    requested_benchmark = config.benchmark or bundle.config.required_benchmarks[0]
    calendar = tuple(
        value for value in bundle.trading_calendar if requested_start <= value <= requested_end
    )
    if not calendar:
        raise ValueError("requested backtest range is outside the snapshot calendar")
    if (bundle.trading_calendar[0] - requested_start).days > 7:
        raise ValueError("requested start_date predates the snapshot calendar")
    if (requested_end - bundle.trading_calendar[-1]).days > 7:
        raise ValueError("requested end_date exceeds the snapshot calendar")
    benchmark_name = _benchmark_name(requested_benchmark)
    if benchmark_name not in set(bundle.config.required_benchmarks):
        raise ValueError(f"requested benchmark is absent from snapshot: {requested_benchmark}")
    selected_dates = set(calendar)
    selected_config = bundle.config.model_copy(
        update={
            "initial_cash": initial_capital,
        }
    )
    return BacktestBundle(
        config=selected_config,
        trading_calendar=calendar,
        bars=tuple(item for item in bundle.bars if item.trading_date in selected_dates),
        rules=tuple(item for item in bundle.rules if item.trading_date in selected_dates),
        signals=tuple(item for item in bundle.signals if item.signal_date in selected_dates),
        adv_amounts=tuple(
            item for item in bundle.adv_amounts if item.trading_date in selected_dates
        ),
        volatilities=tuple(
            item for item in bundle.volatilities if item.trading_date in selected_dates
        ),
        benchmarks=tuple(
            item.model_copy(
                update={
                    "returns": {
                        value_date: value
                        for value_date, value in item.returns.items()
                        if value_date in selected_dates
                    }
                }
            )
            for item in bundle.benchmarks
        ),
    )


def _benchmark_name(value: str) -> str:
    return {
        "000300.SH": "CSI300",
        "000905.SH": "CSI500",
        "EQUAL_WEIGHT_UNIVERSE": "EQUAL_WEIGHT_UNIVERSE",
    }.get(value, value)


def _put_json(store: LocalObjectStore, value: Any) -> dict[str, str]:
    uri, digest = store.put(canonical_json(value), content_type="application/json")
    return {"uri": uri, "sha256": digest}


def _require_unique(values: Sequence[Any] | Any, label: str) -> None:
    materialized = list(values)
    if len(set(materialized)) != len(materialized):
        raise ValueError(f"duplicate {label} in fixed-snapshot bundle")


def _exactly_one(values: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    if len(values) != 1:
        raise ValueError(f"bundle requires exactly one {kind} row, got {len(values)}")
    return values[0]


def _local_path(uri: str) -> Path:
    if uri.startswith("file://"):
        parsed = urlparse(uri)
        raw_path = unquote(parsed.path)
        if len(raw_path) >= 3 and raw_path[0] == "/" and raw_path[2] == ":":
            raw_path = raw_path[1:]
        path = Path(raw_path)
    elif "://" in uri:
        raise ValueError(f"built-in executor only supports local Parquet snapshots: {uri}")
    else:
        path = Path(uri)
    if not path.is_file():
        raise ValueError(f"snapshot file does not exist: {path}")
    return path


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
