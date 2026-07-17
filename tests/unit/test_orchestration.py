from __future__ import annotations

from datetime import date

import ashare_ai.orchestration.daily as daily_module
from ashare_ai.orchestration.daily import (
    daily_research_flow,
    load_pipeline,
    scheduled_daily_research_flow,
)


class FakePipeline:
    def __init__(self, risk_state: str = "NORMAL") -> None:
        self.risk = risk_state
        self.calls: list[str] = []

    def _record(self, name: str, result):
        self.calls.append(name)
        return result

    def start_run(self, trading_date):
        return self._record("start", "run")

    def sync_reference_data(self, run_id):
        return self._record("sync", None)

    def ingest_and_verify(self, run_id):
        return self._record("ingest", ["snapshot"])

    def build_universe(self, run_id, snapshot_ids):
        return self._record("universe", "universe")

    def build_features(self, run_id, universe_id):
        return self._record("features", "features")

    def run_research_agents(self, run_id, feature_snapshot_id):
        return self._record("agents", "agents")

    def calculate_scores(self, run_id, agent_bundle_id):
        return self._record("scores", "scores")

    def qlib_filter(self, run_id, score_snapshot_id):
        return self._record("qlib", "candidates")

    def risk_state(self, run_id):
        return self._record("risk", self.risk)

    def build_portfolio(self, run_id, candidate_snapshot_id):
        return self._record("portfolio", "portfolio")

    def publish_report(self, run_id, portfolio_id, risk_state):
        return self._record("report", "report")

    def complete_run(self, run_id, report_id, status):
        return self._record("complete", {"status": status})


def test_flow_orders_tasks_and_skips_portfolio_when_fused() -> None:
    pipeline = FakePipeline("OBSERVE_ONLY")
    result = daily_research_flow(date(2026, 7, 14), pipeline)
    assert result == {"status": "FUSED"}
    assert pipeline.calls == [
        "start",
        "sync",
        "ingest",
        "universe",
        "features",
        "agents",
        "scores",
        "qlib",
        "risk",
        "report",
        "complete",
    ]


def create_test_pipeline() -> FakePipeline:
    return FakePipeline()


def test_pipeline_factory_is_loaded_by_import_path() -> None:
    pipeline = load_pipeline("test_orchestration:create_test_pipeline")
    assert isinstance(pipeline, FakePipeline)


def test_scheduled_flow_passes_only_serializable_stage_values(monkeypatch) -> None:
    monkeypatch.setenv("ASHARE_PIPELINE_FACTORY", "test_orchestration:create_test_pipeline")
    flow_function = getattr(scheduled_daily_research_flow, "fn", scheduled_daily_research_flow)
    task_function = getattr(daily_module._configured_call, "fn", daily_module._configured_call)
    monkeypatch.setattr(daily_module, "_configured_call", task_function)
    result = flow_function(date(2026, 7, 14))
    assert result == {"status": "SUCCEEDED"}
