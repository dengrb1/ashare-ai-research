"""Restricted line-delimited JSON server for isolated live AKShare calls."""

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
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


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
            str(payload.get("adjustment", "hfq")),
        )
    raise ValueError("unsupported operation")


def _response(request_id: object, payload: object, provider: _AKShareInProcessProvider) -> bytes:
    try:
        items = handle_request(payload, provider=provider)
        response = {"id": request_id, "ok": True, "items": items}
    except Exception:
        # Provider exceptions can contain upstream URLs or parameters and must
        # never cross this process boundary.
        response = {"id": request_id, "ok": False}
    encoded = json.dumps(
        response,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_RESPONSE_BYTES:
        encoded = json.dumps({"id": request_id, "ok": False}, separators=(",", ":")).encode()
    return encoded + b"\n"


def main() -> None:
    provider = _AKShareInProcessProvider()
    try:
        provider._sdk()  # Deliberate one-time heavy-runtime warmup.
    except Exception:
        raise SystemExit(1) from None
    sys.stdout.buffer.write(b'{"ready":true}\n')
    sys.stdout.buffer.flush()
    for raw in sys.stdin.buffer:
        if not raw or len(raw) > MAX_REQUEST_BYTES:
            raise SystemExit(2)
        request_id: object = None
        try:
            envelope = json.loads(raw)
            if not isinstance(envelope, dict):
                raise ValueError("request envelope must be an object")
            request_id = envelope.get("id")
            if not isinstance(request_id, int) or isinstance(request_id, bool):
                raise ValueError("invalid request id")
            payload = envelope.get("payload")
        except Exception:
            raise SystemExit(2) from None
        sys.stdout.buffer.write(_response(request_id, payload, provider))
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
