"""Restricted JSON entrypoint for isolated live AKShare calls."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from typing import Any

from ashare_ai.market.service import (
    MAX_PREFETCH_SYMBOLS,
    _AKShareInProcessProvider,
    normalize_symbol,
)

MAX_REQUEST_BYTES = 64 * 1024
MAX_KLINE_ITEMS = 5000


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("invalid datetime")
    return datetime.fromisoformat(value)


def handle_request(
    payload: object, *, provider: _AKShareInProcessProvider | None = None
) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError("request must be an object")
    operation = payload.get("operation")
    effective = provider or _AKShareInProcessProvider()
    if operation == "quotes":
        raw_symbols = payload.get("symbols")
        if not isinstance(raw_symbols, list) or not 1 <= len(raw_symbols) <= MAX_PREFETCH_SYMBOLS:
            raise ValueError("invalid quote symbols")
        if not all(isinstance(symbol, str) and len(symbol) <= 16 for symbol in raw_symbols):
            raise ValueError("invalid quote symbol")
        symbols = sorted({normalize_symbol(symbol) for symbol in raw_symbols})
        requested = set(symbols)
        rows = effective.quotes(symbols)
        return [row for row in rows if row.get("symbol") in requested]
    if operation == "klines":
        symbol = payload.get("symbol")
        period = payload.get("period")
        limit = payload.get("limit")
        if not isinstance(symbol, str) or len(symbol) > 16:
            raise ValueError("invalid kline symbol")
        if period not in {"1", "5", "15", "30", "60", "daily"}:
            raise ValueError("invalid kline period")
        if (
            not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= MAX_KLINE_ITEMS
        ):
            raise ValueError("invalid kline limit")
        return effective.klines(
            normalize_symbol(symbol),
            period,
            _optional_datetime(payload.get("start")),
            _optional_datetime(payload.get("end")),
            limit,
        )
    raise ValueError("unsupported operation")


def main() -> None:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if not raw or len(raw) > MAX_REQUEST_BYTES:
        raise SystemExit(2)
    try:
        payload = json.loads(raw)
        items = handle_request(payload)
        encoded = json.dumps(
            {"ok": True, "items": items},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except Exception:
        # The parent only needs a stable failure signal. Provider exceptions can
        # contain upstream URLs or parameters and must not cross this boundary.
        raise SystemExit(1) from None
    sys.stdout.write(encoded)


if __name__ == "__main__":
    main()
