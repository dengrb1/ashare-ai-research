"""Local real-time quote bridge for the A-share AI Trader paper desk.

Serves the HttpJsonBridgeSource contract documented in
docs/DATA_SOURCES.md / a_share_ai_trader/market_sources.py:

    GET /quote?symbol=600690.SH           -> single quote object
    GET /quote?symbols=600690.SH,600330.SH -> {symbol: quote, ...}

Quote object fields: symbol, last_price, bid1, ask1, previous_close,
volume (shares), amount (CNY), upper_limit, lower_limit, open, high, low.

Upstreams (in order): Tencent qt.gtimg.cn (primary, no auth, real-time),
Sina hq.sinajs.cn (fallback, requires Referer). Both are free public
end-of-day/real-time quotes; during A-share trading hours they update
continuously. Responses are cached briefly to avoid hammering upstreams.

Stdlib only: python tools/quote_bridge/server.py [--port 8081]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SYMBOL_PATTERN = re.compile(r"^(\d{6})\.(SH|SZ)$")

TENCENT_URL = "https://qt.gtimg.cn/q={prefix}{code}"
SINA_URL = "https://hq.sinajs.cn/list={prefix}{code}"
SINA_HEADERS = {"Referer": "https://finance.sina.com.cn"}

CACHE_TTL_SECONDS = 0.6
UPSTREAM_TIMEOUT_SECONDS = 4.0
USER_AGENT = "a-share-quote-bridge/1.0"


class QuoteError(Exception):
    pass


def _split_symbol(symbol: str) -> tuple[str, str]:
    """Return (exchange_prefix, code) for 600690.SH -> ("sh", "600690")."""
    match = SYMBOL_PATTERN.fullmatch(symbol)
    if not match:
        raise QuoteError(f"invalid symbol {symbol!r}")
    code, exchange = match.groups()
    return exchange.lower(), code


def _to_prefix(symbol: str) -> str:
    return _split_symbol(symbol)[0]


def _decode_gbk(data: bytes) -> str:
    return data.decode("gbk", errors="replace")


def _http_get(url: str, headers: dict | None = None) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, **(headers or {})},
    )
    with urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT_SECONDS) as response:
        return response.read()


# ---------------------------------------------------------------- upstreams

def _parse_tencent(text: str, symbol: str) -> dict:
    """Parse a Tencent qt.gtimg.cn line: v_sh600690="1~name~code~...";"""
    prefix, code = _split_symbol(symbol)
    marker = f'v_{prefix}{code}="'
    start = text.find(marker)
    if start < 0:
        raise QuoteError("tencent: symbol not found in response")
    start += len(marker)
    end = text.find('"', start)
    if end < 0:
        raise QuoteError("tencent: malformed payload")
    fields = text[start:end].split("~")
    if len(fields) < 49:
        raise QuoteError("tencent: too few fields")
    price = _to_float(fields[3])
    previous_close = _to_float(fields[4])
    if not price or not previous_close:
        raise QuoteError("tencent: no valid price")
    return {
        "symbol": symbol,
        "last_price": price,
        "previous_close": previous_close,
        "open": _to_float(fields[5]),
        "volume": _to_float(fields[6]) * 100.0,  # 手 -> 股
        "bid1": _to_float(fields[9]),
        "ask1": _to_float(fields[19]),
        "high": _to_float(fields[33]),
        "low": _to_float(fields[34]),
        "amount": _to_float(fields[37]) * 10_000.0,  # 万元 -> 元
        "upper_limit": _to_float(fields[47]),
        "lower_limit": _to_float(fields[48]),
        "quote_time": fields[30],
    }


def _parse_sina(text: str, symbol: str) -> dict:
    """Parse a Sina hq.sinajs.cn line: var hq_str_sh600690="name,open,...";"""
    prefix, code = _split_symbol(symbol)
    marker = f'hq_str_{prefix}{code}="'
    start = text.find(marker)
    if start < 0:
        raise QuoteError("sina: symbol not found in response")
    start += len(marker)
    end = text.find('"', start)
    if end < 0:
        raise QuoteError("sina: malformed payload")
    fields = text[start:end].split(",")
    if len(fields) < 32:
        raise QuoteError("sina: too few fields")
    price = _to_float(fields[3])
    previous_close = _to_float(fields[2])
    if not price or not previous_close:
        raise QuoteError("sina: no valid price")
    return {
        "symbol": symbol,
        "last_price": price,
        "previous_close": previous_close,
        "open": _to_float(fields[1]),
        "volume": _to_float(fields[8]),  # 股
        "bid1": _to_float(fields[6]),
        "ask1": _to_float(fields[7]),
        "high": _to_float(fields[4]),
        "low": _to_float(fields[5]),
        "amount": _to_float(fields[9]),  # 元
        "upper_limit": round(previous_close * 1.10, 2),
        "lower_limit": round(previous_close * 0.90, 2),
        "quote_time": f"{fields[30]} {fields[31]}".strip(),
    }


def fetch_quote(symbol: str) -> dict:
    prefix, code = _split_symbol(symbol)
    last_error: QuoteError | None = None
    try:
        raw = _http_get(TENCENT_URL.format(prefix=prefix, code=code))
        return _parse_tencent(_decode_gbk(raw), symbol)
    except QuoteError as exc:
        last_error = exc
    except Exception as exc:  # network / HTTP errors
        last_error = QuoteError(f"tencent: {exc}")
    try:
        raw = _http_get(
            SINA_URL.format(prefix=prefix, code=code), headers=SINA_HEADERS
        )
        return _parse_sina(_decode_gbk(raw), symbol)
    except QuoteError as exc:
        raise QuoteError(f"{last_error}; sina fallback: {exc}") from exc
    except Exception as exc:
        raise QuoteError(f"{last_error}; sina fallback: {exc}") from exc


# ---------------------------------------------------------------- cache

class QuoteCache:
    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._items: dict[str, tuple[float, dict | Exception]] = {}

    def get_or_fetch(self, symbol: str) -> dict:
        now = time.monotonic()
        with self._lock:
            cached = self._items.get(symbol)
            if cached and now - cached[0] < self._ttl:
                value = cached[1]
                if isinstance(value, Exception):
                    raise value
                return dict(value)
        try:
            quote = fetch_quote(symbol)
        except Exception as exc:  # keep the failure briefly cached too
            with self._lock:
                self._items[symbol] = (time.monotonic(), exc)
            raise
        with self._lock:
            self._items[symbol] = (time.monotonic(), quote)
        return quote


# ---------------------------------------------------------------- http server

CACHE = QuoteCache(CACHE_TTL_SECONDS)


class BridgeHandler(BaseHTTPRequestHandler):
    server_version = "A-Share-Quote-Bridge/1.0"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/health":
            self._send_json(200, {"status": "ok", "time": time.time()})
            return
        if parsed.path == "/":
            self._send_json(
                200,
                {
                    "service": "a-share-quote-bridge",
                    "usage": "/quote?symbol=600690.SH or ?symbols=a,b",
                },
            )
            return
        if parsed.path != "/quote":
            self._send_json(404, {"error": "not found"})
            return
        query = urllib.parse.parse_qs(parsed.query)
        raw_symbols = query.get("symbols", [""])[0] or query.get("symbol", [""])[0]
        symbols = [
            item.strip().upper()
            for item in raw_symbols.split(",")
            if item.strip()
        ]
        if not symbols:
            self._send_json(400, {"error": "missing symbol"})
            return
        try:
            for symbol in symbols:
                _to_prefix(symbol)  # validate
        except QuoteError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        if len(symbols) == 1 and "symbols" not in query:
            try:
                quote = CACHE.get_or_fetch(symbols[0])
            except QuoteError as exc:
                self._send_json(502, {"error": str(exc)})
                return
            self._send_json(200, quote)
            return
        result: dict[str, dict] = {}
        for symbol in symbols:
            try:
                result[symbol] = CACHE.get_or_fetch(symbol)
            except QuoteError:
                result[symbol] = {"symbol": symbol, "error": "unavailable"}
        self._send_json(200, result)


def _to_float(value: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(description="A-share real-time quote bridge")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), BridgeHandler)
    print(f"quote bridge listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
