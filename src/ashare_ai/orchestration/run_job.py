from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any


def load_handler(kind: str) -> Callable[[str], Any]:
    if kind == "schedule":
        from ashare_ai.orchestration.research_schedule import dispatch_auto_research

        return lambda _job_id: dispatch_auto_research()
    if kind == "research":
        from ashare_ai.orchestration.research_jobs import run_research_job

        return run_research_job
    if kind == "trade-plan":
        from ashare_ai.orchestration.trade_plan_jobs import run_trade_plan_job

        return run_trade_plan_job
    if kind == "backtest":
        from ashare_ai.orchestration.backtest_jobs import run_backtest_job

        return run_backtest_job
    raise ValueError(f"unsupported job kind: {kind}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Execute one leased background job")
    parser.add_argument("kind", choices=("schedule", "research", "trade-plan", "backtest"))
    parser.add_argument("job_id")
    arguments = parser.parse_args()
    load_handler(arguments.kind)(arguments.job_id)


if __name__ == "__main__":
    main()
