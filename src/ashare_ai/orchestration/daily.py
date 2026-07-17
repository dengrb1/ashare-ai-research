from __future__ import annotations

import os
from collections.abc import Callable
from datetime import date, datetime
from importlib import import_module
from typing import Any, Protocol, TypeVar, cast

from ashare_ai.core.config import get_settings
from ashare_ai.core.time import SHANGHAI

F = TypeVar("F", bound=Callable[..., Any])
_prefect_flow: Callable[..., Any] | None
_prefect_task: Callable[..., Any] | None
try:
    from prefect import flow as _flow_implementation
    from prefect import task as _task_implementation

    _prefect_flow = cast(Callable[..., Any], _flow_implementation)
    _prefect_task = cast(Callable[..., Any], _task_implementation)
except ImportError:  # Core/test environments do not require the heavy orchestration extra.
    _prefect_flow = None
    _prefect_task = None


def _decorator(
    implementation: Callable[..., Any] | None, *args: Any, **kwargs: Any
) -> Callable[[F], F]:
    if implementation is None:
        return lambda function: function
    return cast(Callable[[F], F], implementation(*args, **kwargs))


def task(*args: Any, **kwargs: Any) -> Callable[[F], F]:
    return _decorator(_prefect_task, *args, **kwargs)


def flow(*args: Any, **kwargs: Any) -> Callable[[F], F]:
    return _decorator(_prefect_flow, *args, **kwargs)


class Pipeline(Protocol):
    def start_run(self, trading_date: date) -> str: ...
    def sync_reference_data(self, run_id: str) -> None: ...
    def ingest_and_verify(self, run_id: str) -> list[str]: ...
    def build_universe(self, run_id: str, snapshot_ids: list[str]) -> str: ...
    def build_features(self, run_id: str, universe_id: str) -> str: ...
    def run_research_agents(self, run_id: str, feature_snapshot_id: str) -> str: ...
    def calculate_scores(self, run_id: str, agent_bundle_id: str) -> str: ...
    def qlib_filter(self, run_id: str, score_snapshot_id: str) -> str: ...
    def risk_state(self, run_id: str) -> str: ...
    def build_portfolio(self, run_id: str, candidate_snapshot_id: str) -> str | None: ...
    def publish_report(self, run_id: str, portfolio_id: str | None, risk_state: str) -> str: ...
    def complete_run(self, run_id: str, report_id: str, status: str) -> dict[str, Any]: ...


def daily_research_flow(trading_date: date, pipeline: Pipeline) -> dict[str, Any]:
    """Strict task order; implementations exchange IDs instead of large frames."""
    run_id = pipeline.start_run(trading_date)
    pipeline.sync_reference_data(run_id)
    snapshots = pipeline.ingest_and_verify(run_id)
    universe_id = pipeline.build_universe(run_id, snapshots)
    feature_snapshot_id = pipeline.build_features(run_id, universe_id)
    agent_bundle_id = pipeline.run_research_agents(run_id, feature_snapshot_id)
    score_snapshot_id = pipeline.calculate_scores(run_id, agent_bundle_id)
    candidate_snapshot_id = pipeline.qlib_filter(run_id, score_snapshot_id)
    risk_state = pipeline.risk_state(run_id)
    portfolio_id = None
    final_status = "FUSED" if risk_state == "OBSERVE_ONLY" else "SUCCEEDED"
    if risk_state != "OBSERVE_ONLY":
        portfolio_id = pipeline.build_portfolio(run_id, candidate_snapshot_id)
    report_id = pipeline.publish_report(run_id, portfolio_id, risk_state)
    return pipeline.complete_run(run_id, report_id, final_status)


def load_pipeline(factory_path: str | None = None) -> Pipeline:
    path = (
        factory_path
        or os.environ.get("ASHARE_PIPELINE_FACTORY")
        or get_settings().ashare_pipeline_factory
    )
    if not path or ":" not in path:
        raise RuntimeError(
            "ASHARE_PIPELINE_FACTORY must be set to 'package.module:create_pipeline'"
        )
    module_name, attribute = path.rsplit(":", 1)
    factory = getattr(import_module(module_name), attribute)
    pipeline = factory()
    return cast(Pipeline, pipeline)


@flow(name="ashare-after-close-scheduled", log_prints=True)
def scheduled_daily_research_flow(trading_date: date | None = None) -> dict[str, Any]:
    effective_date = trading_date or datetime.now(SHANGHAI).date()
    run_id = _configured_call("start_run", effective_date)
    _configured_call("sync_reference_data", run_id)
    snapshots = _configured_call("ingest_and_verify", run_id)
    universe_id = _configured_call("build_universe", run_id, snapshots)
    feature_snapshot_id = _configured_call("build_features", run_id, universe_id)
    agent_bundle_id = _configured_call("run_research_agents", run_id, feature_snapshot_id)
    score_snapshot_id = _configured_call("calculate_scores", run_id, agent_bundle_id)
    candidate_snapshot_id = _configured_call("qlib_filter", run_id, score_snapshot_id)
    risk_state = _configured_call("risk_state", run_id)
    portfolio_id = None
    final_status = "FUSED" if risk_state == "OBSERVE_ONLY" else "SUCCEEDED"
    if risk_state != "OBSERVE_ONLY":
        portfolio_id = _configured_call("build_portfolio", run_id, candidate_snapshot_id)
    report_id = _configured_call("publish_report", run_id, portfolio_id, risk_state)
    return cast(
        dict[str, Any],
        _configured_call("complete_run", run_id, report_id, final_status),
    )


@task(retries=2, retry_delay_seconds=30)
def _configured_call(method_name: str, *args: Any) -> Any:
    """Load the configured pipeline inside the task; only serializable IDs cross task edges."""
    pipeline = load_pipeline()
    method = getattr(pipeline, method_name)
    return method(*args)
