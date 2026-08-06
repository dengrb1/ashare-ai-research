from __future__ import annotations

import os
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from importlib import import_module
from pathlib import Path
from typing import Any, Protocol, TypeVar, cast
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from ashare_ai.agents.model_settings import ModelConfigurationService
from ashare_ai.core.config import get_settings
from ashare_ai.core.hashing import sha256_bytes, stable_hash
from ashare_ai.core.system_settings import SystemConfigurationService
from ashare_ai.core.time import SHANGHAI, market_decision_time
from ashare_ai.core.user_errors import public_error_message
from ashare_ai.observability.audit import AuditLogger
from ashare_ai.storage.database import SessionLocal
from ashare_ai.storage.models import JobRun

T = TypeVar("T")


def _error_details(error: Exception, **base: Any) -> dict[str, Any]:
    details = {**base, "error_type": type(error).__name__}
    audit_details = getattr(error, "audit_details", None)
    if callable(audit_details):
        value = audit_details()
        if isinstance(value, dict):
            details.update(value)
    return details


def _execution_id() -> str:
    try:
        from prefect.runtime import flow_run

        runtime_id = flow_run.id
    except (ImportError, RuntimeError):
        runtime_id = None
    return str(runtime_id or uuid4())


def _elapsed_ms(started: float) -> int:
    """Milliseconds elapsed on the monotonic clock since ``started``."""
    return round((time.perf_counter() - started) * 1000)


class PipelineBackend(Protocol):
    """Production data/model implementation behind the stable control-plane pipeline."""

    def run_manifest(self, trading_date: date, decision_at: datetime) -> dict[str, Any]: ...
    def sync_reference_data(self, run_id: str) -> None: ...
    def ingest_and_verify(self, run_id: str) -> list[str]: ...
    def build_universe(self, run_id: str, snapshot_ids: list[str]) -> str: ...
    def build_features(self, run_id: str, universe_id: str) -> str: ...
    def run_research_agents(self, run_id: str, feature_snapshot_id: str) -> str: ...
    def calculate_scores(self, run_id: str, agent_bundle_id: str) -> str: ...
    def qlib_filter(self, run_id: str, score_snapshot_id: str) -> str: ...
    def risk_state(self, run_id: str) -> str: ...
    def portfolio_requested(self, run_id: str) -> bool: ...
    def build_portfolio(self, run_id: str, candidate_snapshot_id: str) -> str | None: ...
    def publish_report(self, run_id: str, portfolio_id: str | None, risk_state: str) -> str: ...


class ApplicationPipeline:
    """Concrete auditable Pipeline; business stages are injected as one versioned backend."""

    def __init__(
        self,
        backend: PipelineBackend,
        session_factory: Callable[[], Session] = SessionLocal,
    ) -> None:
        self.backend = backend
        self.session_factory = session_factory

    def start_run(self, trading_date: date) -> str:
        backend_settings = getattr(self.backend, "_settings", None)
        settings = backend_settings if backend_settings is not None else get_settings()
        decision_at = market_decision_time(
            trading_date, settings.decision_hour, settings.decision_minute
        )
        current = datetime.now(SHANGHAI)
        if trading_date > current.date():
            raise RuntimeError("daily research trading_date cannot be in the future")
        if trading_date == current.date():
            decision_at = current
            research_start = (
                settings.daily_research_start_hour,
                settings.daily_research_start_minute,
            )
            if (
                settings.canonical_bundle_mode == "akshare"
                and (current.hour, current.minute) < research_start
            ):
                earliest = (
                    f"{settings.daily_research_start_hour:02d}:"
                    f"{settings.daily_research_start_minute:02d}"
                )
                raise RuntimeError(f"AKShare 当日研究需在收盘后 {earliest}（Asia/Shanghai）启动")
        execution_id = _execution_id()
        run_id = str(uuid5(NAMESPACE_URL, f"ashare-daily:{execution_id}"))
        manifest = dict(self.backend.run_manifest(trading_date, decision_at))
        lock_path = Path(os.environ.get("DEPENDENCY_LOCK_PATH", "requirements.lock"))
        policy_path = settings.policy_config_path
        if not policy_path.exists():
            raise FileNotFoundError(f"production policy config not found: {policy_path}")
        manifest.update(
            code_git_sha=os.environ.get("GIT_SHA", "UNVERSIONED"),
            config_sha256=stable_hash(settings.model_dump(mode="json")),
            dependency_lock_sha256=(
                sha256_bytes(lock_path.read_bytes()) if lock_path.exists() else "UNAVAILABLE"
            ),
            policy_config_path=str(policy_path),
            policy_config_sha256=sha256_bytes(policy_path.read_bytes()),
            random_seed=int(manifest.get("random_seed", 42)),
        )
        with self.session_factory() as session:
            system_configuration = SystemConfigurationService(settings).resolve(session)
            manifest["system_configuration"] = system_configuration.manifest_reference()
            model_configuration = ModelConfigurationService(settings).resolve(session)
            manifest["model_configuration"] = (
                model_configuration.manifest_reference()
                if model_configuration is not None
                else {
                    "source": "builtin",
                    "configuration_id": None,
                    "version": 0,
                    "config_sha256": stable_hash({"agent_backend": "builtin"}),
                    "enabled": False,
                }
            )
            input_hash = stable_hash(manifest)
            idempotency_key = stable_hash({"execution_id": execution_id, "input_hash": input_hash})
            existing = session.scalar(
                select(JobRun).where(JobRun.idempotency_key == idempotency_key)
            )
            if existing is not None:
                return existing.run_id
            session.add(
                JobRun(
                    run_id=run_id,
                    run_type="DAILY",
                    trading_date=trading_date,
                    decision_at=decision_at,
                    status="RUNNING",
                    idempotency_key=idempotency_key,
                    manifest=manifest,
                    input_hash=input_hash,
                    started_at=datetime.now(UTC),
                )
            )
            session.flush()
            AuditLogger(session).record(
                run_id,
                "RUN_STARTED",
                "After-close research run started",
                details={
                    "input_hash": input_hash,
                    "decision_at": decision_at.isoformat(),
                    "system_configuration": system_configuration.manifest_reference(),
                },
            )
            session.commit()
        return run_id

    def _stage(self, run_id: str, name: str, function: Callable[[], T]) -> T:
        # Monotonic wall clock for stage duration; audit timestamps are created
        # separately and must not be used to approximate stage boundaries.
        started = time.perf_counter()
        try:
            result = function()
        except Exception as exc:
            duration_ms = _elapsed_ms(started)
            with self.session_factory() as session:
                run = session.get(JobRun, run_id)
                if run is not None:
                    cancel_requested = run.status == "CANCEL_REQUESTED"
                    if not cancel_requested:
                        run.status = "FAILED"
                        run.error_message = public_error_message("RESEARCH_FAILED")
                        run.completed_at = datetime.now(UTC)
                    AuditLogger(session).record(
                        run_id,
                        "STAGE_FAILED",
                        f"Pipeline stage failed: {name}",
                        severity="WARNING" if cancel_requested else "ERROR",
                        details={
                            **_error_details(exc, stage=name),
                            "duration_ms": duration_ms,
                            **({"cancel_requested": True} if cancel_requested else {}),
                        },
                    )
                    session.commit()
            raise
        with self.session_factory() as session:
            AuditLogger(session).record(
                run_id,
                "STAGE_COMPLETED",
                f"Pipeline stage completed: {name}",
                details={
                    "stage": name,
                    "duration_ms": _elapsed_ms(started),
                    "output_hash": stable_hash(result),
                },
            )
            session.commit()
        return result

    def sync_reference_data(self, run_id: str) -> None:
        self._stage(run_id, "sync_reference_data", lambda: self.backend.sync_reference_data(run_id))

    def ingest_and_verify(self, run_id: str) -> list[str]:
        return self._stage(
            run_id, "ingest_and_verify", lambda: self.backend.ingest_and_verify(run_id)
        )

    def build_universe(self, run_id: str, snapshot_ids: list[str]) -> str:
        return self._stage(
            run_id, "build_universe", lambda: self.backend.build_universe(run_id, snapshot_ids)
        )

    def build_features(self, run_id: str, universe_id: str) -> str:
        return self._stage(
            run_id, "build_features", lambda: self.backend.build_features(run_id, universe_id)
        )

    def run_research_agents(self, run_id: str, feature_snapshot_id: str) -> str:
        return self._stage(
            run_id,
            "run_research_agents",
            lambda: self.backend.run_research_agents(run_id, feature_snapshot_id),
        )

    def calculate_scores(self, run_id: str, agent_bundle_id: str) -> str:
        return self._stage(
            run_id,
            "calculate_scores",
            lambda: self.backend.calculate_scores(run_id, agent_bundle_id),
        )

    def qlib_filter(self, run_id: str, score_snapshot_id: str) -> str:
        return self._stage(
            run_id, "qlib_filter", lambda: self.backend.qlib_filter(run_id, score_snapshot_id)
        )

    def risk_state(self, run_id: str) -> str:
        return self._stage(run_id, "risk_state", lambda: self.backend.risk_state(run_id))

    def portfolio_requested(self, run_id: str) -> bool:
        method = getattr(self.backend, "portfolio_requested", None)
        return bool(method(run_id)) if callable(method) else True

    def build_portfolio(self, run_id: str, candidate_snapshot_id: str) -> str | None:
        return self._stage(
            run_id,
            "build_portfolio",
            lambda: self.backend.build_portfolio(run_id, candidate_snapshot_id),
        )

    def publish_report(self, run_id: str, portfolio_id: str | None, risk_state: str) -> str:
        return self._stage(
            run_id,
            "publish_report",
            lambda: self.backend.publish_report(run_id, portfolio_id, risk_state),
        )

    def complete_run(self, run_id: str, report_id: str, status: str) -> dict[str, Any]:
        output = {"run_id": run_id, "report_id": report_id, "status": status}
        with self.session_factory() as session:
            run = session.get(JobRun, run_id)
            if run is None:
                raise KeyError(run_id)
            if run.status in {"CANCEL_REQUESTED", "CANCELLED"}:
                run.status = "CANCELLED"
                run.active_research_key = None
                run.error_message = None
                run.completed_at = datetime.now(UTC)
                AuditLogger(session).record(
                    run_id,
                    "RESEARCH_CANCELLED",
                    "Daily research stopped before final completion",
                    details={"boundary": "complete_run"},
                )
                session.commit()
                return {"run_id": run_id, "status": "CANCELLED"}
            run.status = status
            run.error_message = None
            run.output_hash = stable_hash(output)
            run.completed_at = datetime.now(UTC)
            AuditLogger(session).record(
                run_id,
                "RUN_COMPLETED",
                "After-close research run completed",
                details={"status": status, "report_id": report_id},
            )
            session.commit()
        return output


def _load_backend(factory_path: str) -> PipelineBackend:
    if ":" not in factory_path:
        raise RuntimeError("ASHARE_STAGE_BACKEND_FACTORY must use package.module:create_backend")
    module_name, attribute = factory_path.rsplit(":", 1)
    factory = getattr(import_module(module_name), attribute)
    return cast(PipelineBackend, factory())


def create_pipeline() -> ApplicationPipeline:
    factory_path = (
        os.environ.get("ASHARE_STAGE_BACKEND_FACTORY")
        or get_settings().ashare_stage_backend_factory
    )
    if not factory_path:
        raise RuntimeError("ASHARE_STAGE_BACKEND_FACTORY is required for scheduled production runs")
    return ApplicationPipeline(_load_backend(factory_path))
