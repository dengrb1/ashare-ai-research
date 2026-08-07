# Hotspot baseline — 2026-08-08

## Scope and method

All synthetic measurements use fixed inputs, make no LLM or vendor calls, run at least
three times, and report the median. The comparison baseline is commit `a1711ae`; the
candidate is the working tree produced from `plan.md`. Measurements ran on Windows,
Python 3.11, using the repository `.venv`.

Reproduction command:

```powershell
.\.venv\Scripts\python docs/performance/bench_hotspots.py --iterations 9 --suite trade-plan
.\.venv\Scripts\python docs/performance/bench_hotspots.py --iterations 5 --suite all
.\.venv\Scripts\python docs/performance/research_time_diagnose.py --env .env
```

The benchmark script rejects fewer than three iterations and fails if output hashes
differ across runs. Timing tolerance is ±15% for local reruns; the merge gate compares
paired medians on the same host.

## Fixed-input results

| Benchmark | Fixed input | `a1711ae` median | Candidate median | Change | Output hash |
|---|---|---:|---:|---:|---|
| Trade-plan optimizer | 240 sessions, default 3×3×3×2×4 grid | 264.02 ms | 38.90 ms | **6.79× faster** | `3d6c20b...0d70a5cd` |
| Backtest | 242 sessions × 200 symbols, no signals | 1636.86 ms | 1155.41 ms | **29.4% faster** | `e5608881...bf0ff37` |
| DuckDB lake query | committed 200,000-row generated snapshot | 22.49 ms | 18.19 ms | **19.1% faster** | `7fd357e1...4306345b` |

The backtest peak RSS is dominated by the deliberately materialized 48,400 Pydantic
bars/rules/statistics in the benchmark fixture (candidate median 505.5 MiB). The lake
query process peaked at 196.1 MiB, below the explicit DuckDB `256 MiB` limit. This is a
process RSS observation, not a claim that every allocation is owned by DuckDB.

The snapshot streaming golden fixture contains 70,000 generated rows. Before and after
the change it produces exactly:

- payload SHA-256: `8b20259d58ddfddc5fa23dd5ded21b8162788a476c3860cf46b4a777e38f093d`
- Parquet SHA-256: `72b4977d87f174416b7a05a24cfe589386c1d8cf101e36d987680b701cecdbcc`

## Stage and process observations

The read-only audit diagnostic found the latest four daily runs at 5.1 minutes,
9.7 minutes (failed), 3.01 hours, and 58.4 minutes wall time. On 2026-08-07 the recorded
execution stages totalled 13.1 minutes: Agent/LLM 9.9 minutes, reference-data sync
3.0 minutes, and snapshot freeze 11 seconds. The historical 2026-08-06 run remains the
readiness-storm canary; a post-change production run is required to verify the target of
at most three `DATA_READINESS_RETRY` events because code changes cannot rewrite prior
audit history.

A fresh Python no-op subprocess costs 26.86 ms median over nine starts. This is small
for full jobs, but material for tiny isolated operations and supports keeping workers
long-lived while isolating only heavy jobs.

API import RSS changed only marginally (about 106.0 MiB to 105.8 MiB median), so the
API-specific 20 MiB gate is **not claimed**. The financial-search implementation is now
confirmed absent from `sys.modules` at API startup. The material memory saving comes
from profile-gating the separate searxng container; it is absent from the default stack
and remains available through `--profile search` with `SEARXNG_BASE_URL` configured.

## Rust / serialization gate

`bench_ipc_serialization.py --iterations 5` shows orjson serialization itself is about
8–9× faster (95.35 ms to 10.57 ms near the 8 MiB cap), but this is not an end-to-end
child-process measurement. The required ≥10% end-to-end gate is therefore unproven and
the IPC protocol was not migrated. No feature or metrics Rust kernel was added: the
measured Python optimizations already pass their relevant gates and there is no evidence
that a new cross-runtime contract would improve the full task.

## Interpretation

- Trade-plan generation was the strongest measured Python hotspot and now clears the
  required ≥5× gate while preserving the full-grid golden hash.
- Backtest date partitioning removes the O(days × all mappings) scan and preserves its
  golden output.
- Lake streaming preserves byte-identical hashes and bounds DuckDB memory explicitly.
- Research latency still needs one post-deploy daily run for operational acceptance;
  unit tests cover batched probing and the 5→20→80 minute retry schedule.
