"""Microbenchmark: stdlib json vs orjson for the AKShare IPC response shapes.

Shapes mirror market/akshare_worker.py: line-delimited JSON, compact separators,
ensure_ascii=False, allow_nan=False.  Records wall-clock median of N runs; this
is a *serialization-only* data point, NOT an end-to-end proof.
"""

from __future__ import annotations

import json
import statistics
import time

import orjson


def _kline(symbol: str, index: int) -> dict:
    return {
        "symbol": symbol,
        "period": "daily",
        "adjustment": "hfq",
        "timestamp": f"2026-08-{(index % 28) + 1:02d}T00:00:00+08:00",
        "open": 1234.5 + index,
        "high": 1300.0 + index,
        "low": 1220.0 + index,
        "close": 1288.0 + index,
        "volume": 12_345_678 + index,
        "amount": 1_500_000_000 + index,
    }


def payload(kline_count: int) -> dict:
    return {"id": "req-bench", "ok": True, "items": [_kline("600519.SH", i) for i in range(kline_count)]}


def bench(name: str, obj: dict, *, dumps: callable, runs: int = 30) -> float:
    # warm up once
    dumps(obj)
    samples = []
    for _ in range(runs):
        start = time.perf_counter()
        dumps(obj)
        samples.append((time.perf_counter() - start) * 1000)
    median = statistics.median(samples)
    print(f"{name:32s} median={median:8.3f} ms  (min={min(samples):.3f} max={max(samples):.3f})")
    return median


def stdlib_dumps(obj: dict) -> bytes:
    return json.dumps(obj, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def orjson_dumps(obj: dict) -> bytes:
    # orjson defaults: no spaces, utf-8, rejects non-finite floats (allow_nan=False semantics)
    return orjson.dumps(obj)


if __name__ == "__main__":
    shapes = {
        "small (1 item)": payload(1),
        "5000 klines": payload(5000),
    }
    # Near the 8 MiB response cap: build one large item pool once, then scale the
    # item count by measured bytes-per-item so the stdlib encoding lands just
    # under 8 MiB without an O(n^2) probe loop.
    probe = payload(5000)
    bytes_per_item = len(stdlib_dumps(probe)) / 5000
    near_cap_count = max(1, int((8 * 1024 * 1024) / bytes_per_item))
    near_cap = payload(near_cap_count)
    shapes["near-8MiB cap"] = near_cap

    print(f"near-8MiB payload: {near_cap_count} klines, stdlib size={len(stdlib_dumps(near_cap))/1048576:.2f} MiB\n")
    for shape, obj in shapes.items():
        stdlib_ms = bench(f"stdlib {shape}", obj, dumps=stdlib_dumps)
        orjson_ms = bench(f"orjson {shape}", obj, dumps=orjson_dumps)
        print(f"  -> ratio stdlib/orjson = {stdlib_ms / orjson_ms:.2f}x\n")
