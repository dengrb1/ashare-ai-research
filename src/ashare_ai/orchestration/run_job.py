from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any


def load_handler(kind: str) -> Callable[[str], Any]:
    if kind == "schedule":
        from ashare_ai.orchestration.runner import dispatch_scheduled_tasks

        return lambda _job_id: dispatch_scheduled_tasks()
    if kind == "research":
        from ashare_ai.orchestration.research_jobs import run_research_job

        return run_research_job
    if kind == "trade-plan":
        from ashare_ai.orchestration.trade_plan_jobs import run_trade_plan_job

        return run_trade_plan_job
    if kind == "backtest":
        from ashare_ai.orchestration.backtest_jobs import run_backtest_job

        return run_backtest_job
    if kind == "exit-review":
        from ashare_ai.orchestration.exit_advice_jobs import run_exit_advice_job

        return run_exit_advice_job
    if kind == "personal-archive":
        from ashare_ai.orchestration.personal_archive_jobs import run_personal_archive_job

        return run_personal_archive_job
    if kind == "system2-diagnostic":
        from ashare_ai.orchestration.system2_jobs import run_system2_diagnostic_job

        return run_system2_diagnostic_job
    if kind == "jev-training":
        from ashare_ai.orchestration.jev_training_jobs import run_jev_training_job

        return run_jev_training_job
    if kind == "maintenance":
        from ashare_ai.orchestration.maintenance_jobs import run_maintenance_job

        return run_maintenance_job
    raise ValueError(f"unsupported job kind: {kind}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Execute one leased background job")
    parser.add_argument(
        "kind",
        choices=(
            "schedule",
            "research",
            "trade-plan",
            "backtest",
            "exit-review",
            "personal-archive",
            "system2-diagnostic",
            "jev-training",
            "maintenance",
        ),
    )
    parser.add_argument("job_id")
    arguments = parser.parse_args()
    load_handler(arguments.kind)(arguments.job_id)


if __name__ == "__main__":
    main()
