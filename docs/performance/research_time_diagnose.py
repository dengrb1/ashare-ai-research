"""Read-only diagnosis of recent daily-research wall-clock (no writes).

Reads the runtime database (DATABASE_URL from ``--env`` or the environment) and
prints, for the most recent DAILY research runs:

  1. run summary: date, status, wall-clock
  2. per-stage duration breakdown (audit ``STAGE_COMPLETED.details.duration_ms``)
  3. agent-call summary (agent_calls): count, durations, prompt-cache usage,
     retries — the data that tells where a slow run actually spent its time

The connection string is used but never printed. Nothing is modified.
"""

from __future__ import annotations

import argparse
import os
import statistics
from pathlib import Path
from typing import Any

STAGES = (
    "sync_reference_data",
    "ingest_and_verify",
    "build_universe",
    "build_features",
    "run_research_agents",
    "calculate_scores",
    "qlib_filter",
    "risk_state",
    "build_portfolio",
    "publish_report",
)

STAGE_LABELS = {
    "sync_reference_data": "同步参考数据",
    "ingest_and_verify": "冻结并校验快照",
    "build_universe": "构建可交易池",
    "build_features": "计算研究特征",
    "run_research_agents": "执行结构化研究(Agent/LLM)",
    "calculate_scores": "生成确定性评分",
    "qlib_filter": "筛选候选池",
    "risk_state": "评估组合风控",
    "build_portfolio": "生成模拟组合",
    "publish_report": "发布研究报告",
}


def _database_url(env_path: Path | None) -> str:
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    if env_path is not None:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("DATABASE_URL not found; pass --env <runtime .env> or set the environment")


def _fmt(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.2f}h"


def _stage_table(run_stages: dict[str, dict[str, int]], run_wall: float | None) -> None:
    if not run_stages:
        print("    (无 STAGE_COMPLETED 记录)")
        return
    total = sum(run_stages.values())
    order = sorted(run_stages.items(), key=lambda item: item[1], reverse=True)
    for stage, ms in order:
        label = STAGE_LABELS.get(stage, stage)
        pct = 100.0 * ms / total if total else 0.0
        print(f"    {label:<22} {_fmt(ms / 1000):>7}  {pct:>5.1f}%")
    print(f"    {'（已记录阶段合计）':<22} {_fmt(total / 1000):>7}  100.0%")


def _agent_summary(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("    (无 agent_calls 记录)")
        return
    durations = [int(r["duration_ms"] or 0) for r in rows]
    cache_hits = sum(1 for r in rows if int(r.get("cached_input_tokens") or 0) > 0)
    retries = sum(int(r.get("retry_count") or 0) for r in rows)
    failed = sum(1 for r in rows if (r.get("result_status") or "ok") != "ok")
    print(f"    Agent 调用数: {len(rows)}  "
          f"总时长: {_fmt(sum(durations) / 1000)}  "
          f"中位单次: {_fmt(statistics.median(durations) / 1000) if durations else '-'}  "
          f"最大单次: {_fmt(max(durations) / 1000) if durations else '-'}")
    print(f"    提示词缓存命中(cached_input_tokens>0): {cache_hits}/{len(rows)}  "
          f"重试次数合计: {retries}  非 ok 结果: {failed}")
    by_component: dict[str, list[int]] = {}
    for r in rows:
        by_component.setdefault(str(r["component"]), []).append(int(r["duration_ms"] or 0))
    for component, durations_component in sorted(by_component.items()):
        print(f"    - {component}: n={len(durations_component)} "
              f"中位 {_fmt(statistics.median(durations_component) / 1000)} "
              f"合计 {_fmt(sum(durations_component) / 1000)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily-research time diagnosis (read-only).")
    parser.add_argument("--env", type=Path, default=None, help="runtime .env with DATABASE_URL")
    parser.add_argument(
        "--runs", type=int, default=10, help="number of recent DAILY runs to inspect"
    )
    args = parser.parse_args()

    url = _database_url(args.env)
    from sqlalchemy import create_engine, text

    engine = create_engine(url, pool_pre_ping=True)
    run_rows: list[dict[str, Any]] = []
    stages_by_run: dict[str, dict[str, int]] = {}
    agents_by_run: dict[str, list[dict[str, Any]]] = {}

    with engine.connect() as conn:
        runs = conn.execute(
            text(
                """
                SELECT run_id, trading_date, status, started_at, completed_at
                FROM job_runs
                WHERE run_type = 'DAILY'
                ORDER BY trading_date DESC, started_at DESC
                LIMIT :limit
                """
            ),
            {"limit": args.runs},
        ).mappings().all()
        run_rows = [dict(row) for row in runs]

        for row in runs:
            rid = row["run_id"]
            stage_rows = conn.execute(
                text(
                    """
                    SELECT details FROM audit_events
                    WHERE run_id = :rid AND event_type = 'STAGE_COMPLETED'
                    """
                ),
                {"rid": rid},
            ).mappings().all()
            stage_map: dict[str, int] = {}
            for s in stage_rows:
                details = dict(s["details"] or {})
                stage = details.get("stage")
                duration_ms = details.get("duration_ms")
                if stage and isinstance(duration_ms, (int, float)):
                    stage_map[str(stage)] = int(duration_ms)
            stages_by_run[rid] = stage_map

            agent_rows = conn.execute(
                text(
                    """
                    SELECT component, duration_ms, cached_input_tokens, retry_count, result_status
                    FROM agent_calls
                    WHERE run_id = :rid
                    """
                ),
                {"rid": rid},
            ).mappings().all()
            agents_by_run[rid] = [dict(a) for a in agent_rows]

    if not run_rows:
        print("未找到 DAILY 研究记录，输出数据分布以便定位:")
        with engine.connect() as conn:
            total = conn.execute(text("SELECT count(*) AS c FROM job_runs")).scalar()
            print(f"    job_runs 总数: {total}")
            types = conn.execute(
                text("SELECT run_type, count(*) AS c FROM job_runs GROUP BY run_type")
            ).all()
            for run_type, count in types:
                print(f"    run_type={run_type!r}  count={count}")
            latest = conn.execute(
                text(
                    """
                    SELECT run_id, run_type, trading_date, status, started_at, completed_at
                    FROM job_runs ORDER BY started_at DESC LIMIT 5
                    """
                )
            ).mappings().all()
            for row in latest:
                print(
                    f"    最新: type={row['run_type']!r} date={row['trading_date']} "
                    f"status={row['status']} started={row['started_at']}"
                )
        return 1

    print(f"最近 {len(run_rows)} 次日度研究（只读诊断）\n")
    for row in run_rows:
        started = row["started_at"]
        completed = row["completed_at"]
        wall = None
        if started is not None and completed is not None:
            wall = (completed - started).total_seconds()
        date_str = str(row["trading_date"])
        status = str(row["status"])
        print(f"交易日 {date_str}  状态 {status}  墙钟 {_fmt(wall)}")
        _stage_table(stages_by_run.get(row["run_id"], {}), wall)
        _agent_summary(agents_by_run.get(row["run_id"], []))
        print()

    # Cross-run dominant stage (median share of the recorded stage total).
    totals: dict[str, list[float]] = {}
    for stage_map in stages_by_run.values():
        total = sum(stage_map.values())
        if total <= 0:
            continue
        for stage, ms in stage_map.items():
            totals.setdefault(stage, []).append(100.0 * ms / total)
    if totals:
        print("跨运行阶段占比中位数（定位主导阶段）:")
        for stage, shares in sorted(
            totals.items(), key=lambda item: statistics.median(item[1]), reverse=True
        ):
            label = STAGE_LABELS.get(stage, stage)
            print(f"    {label:<22} {statistics.median(shares):>5.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
