from __future__ import annotations

import json
import math
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from importlib import import_module
from inspect import Parameter, signature
from typing import Any, ClassVar, Protocol, cast

import httpx

from ashare_ai.adapters.symbols import normalize_symbol as canonical_symbol
from ashare_ai.core.config import Settings, get_settings
from ashare_ai.core.runtime_mode import is_after_close, runtime_mode_policy
from ashare_ai.core.system_settings import get_effective_settings
from ashare_ai.core.time import SHANGHAI

PERIODS = {
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "60m": "60",
    "day": "daily",
    "daily": "daily",
}
ADJUSTMENTS = {"raw": "raw", "unadjusted": "raw", "none": "raw", "hfq": "hfq"}
MAX_PREFETCH_SYMBOLS = 50
# Provider calls can outlive a timed-out request. Keep the process-wide pool
# bounded so those residual calls cannot multiply across request-local pools.
_PROVIDER_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="market-provider")

# The A-share trading calendar is one full snapshot from Sina, authoritative for
# months ahead.  Cache it whole (not per requested range) so the submit request,
# the minute-tick scheduler and worker retries all share a single upstream fetch
# and slice the same record locally instead of each calling AKShare.
_CALENDAR_CACHE_KEY = "calendar:v1"
_CALENDAR_CACHE_SECONDS = 6 * 60 * 60
_CALENDAR_FULL_START = date(1990, 1, 1)
_CALENDAR_FULL_END = date(2100, 1, 1)
# The worker protocol bounds a calendar request: the full 1990 -> 2100 span is
# ~40,176 days and ~27k A-share sessions, so the old ~10-year/4096-item caps
# rejected the very fetch the cache is built from.  Keep the worker and the
# service request gate on one shared constant so they cannot drift apart.
MAX_CALENDAR_RANGE_DAYS = 40_200
MAX_CALENDAR_ITEMS = 65_536


def _calendar_range(values: object, start_date: date, end_date: date) -> tuple[date, ...]:
    """Slice a cached full-calendar list down to the requested window."""
    if not isinstance(values, list):
        return ()
    result: list[date] = []
    for raw in values:
        if not isinstance(raw, str):
            continue
        try:
            parsed = date.fromisoformat(raw)
        except ValueError:
            continue
        if start_date <= parsed <= end_date:
            result.append(parsed)
    return tuple(result)


def _market_subprocess_env() -> dict[str, str]:
    allowed = {
        "ARROW_IO_THREADS",
        "HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LC_ALL",
        "MALLOC_ARENA_MAX",
        "MKL_NUM_THREADS",
        "NO_PROXY",
        "NUMEXPR_NUM_THREADS",
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "PATH",
        "PYTHONPATH",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TZ",
        "WINDIR",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
    return {key: value for key, value in os.environ.items() if key in allowed}


class MarketProvider(Protocol):
    source: str

    def quotes(self, symbols: list[str]) -> list[dict[str, Any]]: ...

    def klines(
        self,
        symbol: str,
        period: str,
        start: datetime | None,
        end: datetime | None,
        limit: int,
        adjustment: str = "hfq",
    ) -> list[dict[str, Any]]: ...


def normalize_symbol(value: str) -> str:
    return str(canonical_symbol(value))


def _sina_provider_symbol(value: str) -> str:
    code, exchange = normalize_symbol(value).split(".", 1)
    prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(exchange)
    if prefix is None:
        raise ValueError(f"unsupported Sina exchange: {exchange}")
    return f"{prefix}{code}"


def normalize_adjustment(value: str) -> str:
    adjustment = ADJUSTMENTS.get(str(value).casefold())
    if adjustment is None:
        raise ValueError(f"unsupported adjustment: {value}")
    return adjustment


def _number(value: Any) -> float | None:
    if value is None or value in {"", "-", "--"}:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _valid_ohlc(
    open_price: float | None,
    high: float | None,
    low: float | None,
    close: float | None,
) -> bool:
    """Reject incomplete or internally inconsistent supplier bars."""

    if None in (open_price, high, low, close):
        return False
    assert open_price is not None and high is not None and low is not None and close is not None
    return (
        open_price > 0
        and high > 0
        and low > 0
        and close > 0
        and low <= min(open_price, close)
        and high >= max(open_price, close)
    )


def _timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    else:
        parsed = datetime.fromisoformat(str(value).replace("/", "-"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed


def _intraday_series_is_stale(
    bars: list[dict[str, Any]], period: str, requested_end: datetime | None, now: datetime
) -> bool:
    if not bars:
        return False
    current = now.astimezone(SHANGHAI)
    end = _timestamp(requested_end).astimezone(SHANGHAI) if requested_end else current
    clock = current.time().replace(tzinfo=None)
    market_open = datetime.min.time().replace(hour=9, minute=30)
    morning_close = datetime.min.time().replace(hour=11, minute=30)
    afternoon_open = datetime.min.time().replace(hour=13)
    market_close = datetime.min.time().replace(hour=15)
    if end.date() != current.date() or current.weekday() >= 5:
        return False
    latest = max(_timestamp(item["timestamp"]) for item in bars).astimezone(SHANGHAI)
    if period == "daily":
        daily_ready = datetime.min.time().replace(hour=15, minute=5)
        return clock >= daily_ready and latest.date() < current.date()
    if clock < market_open:
        return False
    grace = timedelta(minutes=max(5, int(period) * 2))
    if clock <= morning_close:
        expected = current
    elif clock < afternoon_open:
        expected = current.replace(hour=11, minute=30, second=0, microsecond=0)
    elif clock <= market_close:
        expected = current
    else:
        expected = current.replace(hour=15, minute=0, second=0, microsecond=0)
    return latest.date() < current.date() or latest < expected - grace


def _merge_adjusted_intraday(
    hfq_bars: list[dict[str, Any]],
    raw_bars: list[dict[str, Any]],
    limit: int,
    *,
    adjustment: str = "hfq",
) -> list[dict[str, Any]] | None:
    hfq_by_time = {str(item.get("timestamp")): item for item in hfq_bars}
    overlap = next(
        (
            (hfq_by_time[str(item.get("timestamp"))], item)
            for item in reversed(raw_bars)
            if str(item.get("timestamp")) in hfq_by_time and float(item.get("close") or 0) > 0
        ),
        None,
    )
    if overlap is None:
        return None
    factor = (
        Decimal("1")
        if adjustment == "raw"
        else Decimal(str(overlap[0]["close"])) / Decimal(str(overlap[1]["close"]))
    )
    merged = {str(item["timestamp"]): dict(item) for item in hfq_bars}
    for raw in raw_bars:
        adjusted = dict(raw)
        for field in ("open", "high", "low", "close"):
            adjusted[field] = float(
                (Decimal(str(raw[field])) * factor).quantize(Decimal("0.000001"))
            )
        merged[str(adjusted["timestamp"])] = adjusted
    return sorted(merged.values(), key=lambda item: str(item["timestamp"]))[-limit:]


def _append_adjusted_daily_quote(
    bars: list[dict[str, Any]],
    quote: dict[str, Any],
    now: datetime,
    limit: int,
    *,
    adjustment: str = "hfq",
) -> list[dict[str, Any]] | None:
    trading_at = quote.get("_trading_at")
    if not bars or trading_at is None:
        return None
    quote_time = _timestamp(trading_at).astimezone(SHANGHAI)
    current = now.astimezone(SHANGHAI)
    if quote_time.date() != current.date():
        return None
    previous_close = _number(quote.get("previous_close"))
    if previous_close is None or previous_close <= 0:
        return None
    prior = max(bars, key=lambda item: _timestamp(item["timestamp"]))
    factor = (
        Decimal("1")
        if adjustment == "raw"
        else Decimal(str(prior["close"])) / Decimal(str(previous_close))
    )
    adjusted: dict[str, Any] = {
        "timestamp": current.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(),
        "volume": _number(quote.get("volume")) or 0.0,
        "amount": _number(quote.get("amount")),
        "turnover_rate": None,
    }
    for target, source in (
        ("open", "open"),
        ("high", "high"),
        ("low", "low"),
        ("close", "price"),
    ):
        raw = _number(quote.get(source))
        if raw is None:
            return None
        adjusted[target] = float((Decimal(str(raw)) * factor).quantize(Decimal("0.000001")))
    merged = {str(item["timestamp"]): dict(item) for item in bars}
    merged[str(adjusted["timestamp"])] = adjusted
    return sorted(merged.values(), key=lambda item: str(item["timestamp"]))[-limit:]


def _mark_kline_cache_hit(value: dict[str, Any]) -> dict[str, Any]:
    raw_status = value.get("status")
    status: dict[str, Any] = dict(raw_status) if isinstance(raw_status, dict) else {}
    return {**value, "status": {**status, "cache_hit": True}}


def _kline_cache_key(
    symbol: str,
    period: str,
    limit: int,
    start: datetime | None,
    end: datetime | None,
    *,
    bucket_seconds: int,
    adjustment: str = "hfq",
) -> str:
    """Match cache identity to supplier precision, not browser timestamp noise."""

    if period == "daily":
        start_key = _timestamp(start).date().isoformat() if start is not None else "default"
        end_key = _timestamp(end).date().isoformat() if end is not None else "latest"
    else:
        def bucket(value: datetime | None, fallback: str) -> str:
            if value is None:
                return fallback
            instant = _timestamp(value)
            seconds = int(instant.timestamp())
            return datetime.fromtimestamp(
                seconds - seconds % max(1, bucket_seconds), tz=instant.tzinfo
            ).isoformat()

        start_key = bucket(start, "default")
        end_key = bucket(end, "latest")
    return f"klines:{symbol}:{period}:{limit}:{start_key}:{end_key}:{adjustment}"


def _canonical_kline_bounds(
    period: str,
    start: datetime | None,
    end: datetime | None,
    *,
    bucket_seconds: int,
) -> tuple[datetime | None, datetime | None]:
    if period == "daily":
        lower = (
            _timestamp(start).replace(hour=0, minute=0, second=0, microsecond=0)
            if start is not None
            else None
        )
        upper = (
            _timestamp(end).replace(hour=23, minute=59, second=59, microsecond=999999)
            if end is not None
            else None
        )
        return lower, upper

    def floor(value: datetime) -> datetime:
        instant = _timestamp(value)
        seconds = int(instant.timestamp())
        return datetime.fromtimestamp(
            seconds - seconds % max(1, bucket_seconds), tz=instant.tzinfo
        )

    lower = floor(start) if start is not None else None
    upper = (
        floor(end) + timedelta(seconds=max(1, bucket_seconds), microseconds=-1)
        if end is not None
        else None
    )
    return lower, upper


def _filter_kline_range(
    value: dict[str, Any], start: datetime | None, end: datetime | None, limit: int
) -> dict[str, Any]:
    """A canonical cache bucket may be wider than this caller's exact range."""

    lower = _timestamp(start) if start is not None else None
    upper = _timestamp(end) if end is not None else None
    daily = value.get("period") == "day"

    def included(item: dict[str, Any]) -> bool:
        timestamp = _timestamp(item.get("timestamp"))
        if daily:
            return (lower is None or timestamp.date() >= lower.date()) and (
                upper is None or timestamp.date() <= upper.date()
            )
        return (lower is None or timestamp >= lower) and (upper is None or timestamp <= upper)

    bars = [
        item
        for item in value.get("bars", [])
        if isinstance(item, dict)
        and included(item)
    ]
    return {**value, "bars": bars[-limit:]}


def _validated_kline_bars(
    bars: object,
    period: str,
    start: datetime | None,
    end: datetime | None,
    limit: int,
) -> list[dict[str, Any]]:
    if not isinstance(bars, list) or not bars or len(bars) > limit:
        raise RuntimeError("provider returned invalid bars")
    lower = _timestamp(start) if start is not None else None
    upper = _timestamp(end) if end is not None else None
    validated: list[dict[str, Any]] = []
    for raw in bars:
        if not isinstance(raw, dict):
            raise RuntimeError("provider returned invalid bars")
        timestamp = _timestamp(raw.get("timestamp"))
        if period == "daily":
            outside = (lower is not None and timestamp.date() < lower.date()) or (
                upper is not None and timestamp.date() > upper.date()
            )
        else:
            outside = (lower is not None and timestamp < lower) or (
                upper is not None and timestamp > upper
            )
        if outside:
            raise RuntimeError("provider returned bars outside the requested range")
        open_price = _number(raw.get("open"))
        high = _number(raw.get("high"))
        low = _number(raw.get("low"))
        close = _number(raw.get("close"))
        volume = _number(raw.get("volume"))
        # A zero price is never a tradable A-share price. Providers sometimes
        # use it for a missing intraday row; retaining it creates a full-height
        # candle in clients and corrupts indicator ranges.
        if not _valid_ohlc(open_price, high, low, close) or volume is None or volume < 0:
            continue
        validated.append(
            {
                **raw,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            }
        )
    if not validated:
        raise RuntimeError("provider returned no valid OHLC bars")
    return sorted(validated, key=lambda item: _timestamp(item["timestamp"]))[-limit:]


class _AKShareInProcessProvider:
    source = "akshare"

    def _sdk(self) -> Any:
        return import_module("akshare")

    def quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        frame = self._sdk().stock_zh_a_spot_em()
        del symbols
        rows: list[dict[str, Any]] = []
        for item in frame.to_dict(orient="records"):
            code = str(item.get("代码", "")).zfill(6)
            if not code.isdigit() or len(code) != 6:
                continue
            symbol = normalize_symbol(code)
            rows.append(
                {
                    "symbol": symbol,
                    "name": item.get("名称"),
                    "price": _number(item.get("最新价")),
                    "change": _number(item.get("涨跌额")),
                    "change_percent": _number(item.get("涨跌幅")),
                    "open": _number(item.get("今开")),
                    "high": _number(item.get("最高")),
                    "low": _number(item.get("最低")),
                    "previous_close": _number(item.get("昨收")),
                    "volume": _number(item.get("成交量")),
                    "amount": _number(item.get("成交额")),
                }
            )
        return rows

    def klines(
        self,
        symbol: str,
        period: str,
        start: datetime | None,
        end: datetime | None,
        limit: int,
        adjustment: str = "hfq",
    ) -> list[dict[str, Any]]:
        code = normalize_symbol(symbol).split(".", 1)[0]
        sdk = self._sdk()
        effective_end = end or datetime.now(SHANGHAI)
        adjustment = normalize_adjustment(adjustment)
        if period == "daily":
            effective_start = start or effective_end - timedelta(days=max(limit * 2, 365))
            try:
                frame = sdk.stock_zh_a_hist(
                    symbol=code,
                    period="daily",
                    start_date=effective_start.strftime("%Y%m%d"),
                    end_date=effective_end.strftime("%Y%m%d"),
                    adjust="hfq" if adjustment == "hfq" else "",
                )
            except Exception:
                # Eastmoney commonly resets connections from Docker egress IPs.
                # AKShare's Sina endpoint is independent and returns equivalent
                # daily OHLC data, so keep the isolated provider usable without
                # leaking the upstream exception across the process boundary.
                frame = sdk.stock_zh_a_daily(
                    symbol=_sina_provider_symbol(symbol),
                    start_date=effective_start.strftime("%Y%m%d"),
                    end_date=effective_end.strftime("%Y%m%d"),
                    adjust="hfq" if adjustment == "hfq" else "",
                )
        else:
            effective_start = start or effective_end - timedelta(days=max(5, limit // 20 + 1))
            frame = sdk.stock_zh_a_hist_min_em(
                symbol=code,
                period=period,
                start_date=effective_start.strftime("%Y-%m-%d %H:%M:%S"),
                end_date=effective_end.strftime("%Y-%m-%d %H:%M:%S"),
                adjust="hfq" if adjustment == "hfq" else "",
            )
        bars = []
        for item in frame.tail(limit).to_dict(orient="records"):
            open_price = _number(item.get("开盘", item.get("open")))
            high = _number(item.get("最高", item.get("high")))
            low = _number(item.get("最低", item.get("low")))
            close = _number(item.get("收盘", item.get("close")))
            volume = _number(item.get("成交量", item.get("volume")))
            if not _valid_ohlc(open_price, high, low, close) or volume is None or volume < 0:
                continue
            bars.append(
                {
                    "timestamp": _timestamp(
                        item.get("时间", item.get("日期", item.get("date")))
                    ).isoformat(),
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                    "amount": _number(item.get("成交额", item.get("amount"))),
                    "turnover_rate": _number(
                        item.get("换手率", item.get("turnover"))
                    ),
                }
            )
        return bars

    def sessions(self, start_date: date, end_date: date) -> list[date]:
        frame = self._sdk().tool_trade_date_hist_sina()
        values: set[date] = set()
        for item in frame.to_dict(orient="records"):
            raw = item.get("trade_date", item.get("日期"))
            try:
                parsed = date.fromisoformat(str(raw).replace("/", "-"))
            except (TypeError, ValueError):
                continue
            if start_date <= parsed <= end_date:
                values.add(parsed)
        return sorted(values)


class _PriorityGate:
    """Serialize provider IPC while letting foreground work overtake queued prefetches."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._active = False
        self._foreground_waiters = 0

    def acquire(self, *, background: bool) -> None:
        with self._condition:
            if not background:
                self._foreground_waiters += 1
            try:
                while self._active or (background and self._foreground_waiters):
                    self._condition.wait()
                self._active = True
            finally:
                if not background:
                    self._foreground_waiters -= 1

    def release(self) -> None:
        with self._condition:
            self._active = False
            self._condition.notify_all()


class AKShareMarketProvider:
    """Keep AKShare's heavy runtime in one supervised, reusable child process."""

    source = "akshare"

    def __init__(self, *, timeout_seconds: float) -> None:
        self.timeout_seconds = max(1.0, timeout_seconds - 0.5)
        self._process: subprocess.Popen[str] | None = None
        self._responses: queue.Queue[str | None] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._process_guard = threading.RLock()
        self._gate = _PriorityGate()
        self._request_id = 0
        self._state = "COLD"
        self._last_error: str | None = None

    @property
    def state(self) -> str:
        with self._process_guard:
            return self._state

    @property
    def last_error(self) -> str | None:
        with self._process_guard:
            return self._last_error

    @staticmethod
    def _read_stdout(
        process: subprocess.Popen[str], responses: queue.Queue[str | None]
    ) -> None:
        stream = process.stdout
        if stream is None:
            responses.put(None)
            return
        try:
            while True:
                line = stream.readline(8 * 1024 * 1024 + 1)
                if not line:
                    break
                if len(line.encode("utf-8")) > 8 * 1024 * 1024:
                    break
                responses.put(line.rstrip("\r\n"))
        finally:
            responses.put(None)

    def _discard_process_locked(self, *, reason: str | None = None) -> None:
        process = self._process
        self._process = None
        self._reader = None
        self._responses = queue.Queue()
        self._state = "DEGRADED" if reason else "COLD"
        self._last_error = reason
        if process is None:
            return
        with suppress(Exception):
            if process.poll() is None:
                process.terminate()
        with suppress(Exception):
            process.wait(timeout=1)
        with suppress(Exception):
            if process.poll() is None:
                process.kill()

    def start(self) -> bool:
        """Warm the child without allowing a provider outage to stop the API."""

        with self._process_guard:
            if (
                self._process is not None
                and self._process.poll() is None
                and self._state == "READY"
            ):
                return True
            self._discard_process_locked()
            try:
                process = subprocess.Popen(
                    [sys.executable, "-m", "ashare_ai.market.akshare_worker"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    bufsize=1,
                    close_fds=True,
                    env=_market_subprocess_env(),
                )
                self._process = process
                self._state = "WARMING"
                responses = self._responses
                reader = threading.Thread(
                    target=self._read_stdout,
                    args=(process, responses),
                    name="akshare-provider-reader",
                    daemon=True,
                )
                self._reader = reader
                reader.start()
                ready = self._responses.get(timeout=self.timeout_seconds)
                if ready != '{"ready":true}':
                    raise RuntimeError("akshare provider warmup failed")
                self._state = "READY"
                self._last_error = None
                return True
            except Exception:
                self._discard_process_locked(reason="warmup failed")
                return False

    def close(self) -> None:
        with self._process_guard:
            self._discard_process_locked()

    def _request(
        self, payload: dict[str, Any], *, maximum_items: int, background: bool = False
    ) -> list[dict[str, Any]]:
        self._gate.acquire(background=background)
        try:
            if not self.start():
                raise RuntimeError("akshare provider unavailable")
            encoded_payload = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
            if len(encoded_payload.encode("utf-8")) > 64 * 1024:
                raise ValueError("akshare provider request is too large")
            with self._process_guard:
                process = self._process
                if process is None or process.stdin is None or process.poll() is not None:
                    self._discard_process_locked(reason="provider process exited")
                    raise RuntimeError("akshare provider process failed")
                self._request_id += 1
                request_id = self._request_id
                encoded = json.dumps(
                    {"id": request_id, "payload": json.loads(encoded_payload)},
                    ensure_ascii=True,
                    separators=(",", ":"),
                )
                try:
                    process.stdin.write(encoded + "\n")
                    process.stdin.flush()
                except Exception as exc:
                    self._discard_process_locked(reason="provider pipe failed")
                    raise RuntimeError("akshare provider process failed") from exc
            try:
                raw_response = self._responses.get(timeout=self.timeout_seconds)
            except queue.Empty as exc:
                with self._process_guard:
                    self._discard_process_locked(reason="provider timed out")
                raise TimeoutError("akshare provider timed out") from exc
            if raw_response is None:
                with self._process_guard:
                    self._discard_process_locked(reason="provider process exited")
                raise RuntimeError("akshare provider process failed")
            try:
                response = json.loads(raw_response)
            except json.JSONDecodeError as exc:
                with self._process_guard:
                    self._discard_process_locked(reason="invalid provider response")
                raise RuntimeError("akshare provider returned invalid JSON") from exc
            if not isinstance(response, dict) or response.get("id") != request_id:
                with self._process_guard:
                    self._discard_process_locked(reason="provider protocol mismatch")
                raise RuntimeError("akshare provider returned an invalid response")
            if response.get("ok") is not True:
                raise RuntimeError("akshare provider returned an error")
            items = response.get("items")
            if not isinstance(items, list) or len(items) > maximum_items:
                with self._process_guard:
                    self._discard_process_locked(reason="invalid provider items")
                raise RuntimeError("akshare provider returned invalid items")
            if not all(isinstance(item, dict) for item in items):
                with self._process_guard:
                    self._discard_process_locked(reason="invalid provider items")
                raise RuntimeError("akshare provider returned invalid items")
            return cast(list[dict[str, Any]], items)
        finally:
            self._gate.release()

    def quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        normalized = sorted({normalize_symbol(symbol) for symbol in symbols})
        if len(normalized) > MAX_PREFETCH_SYMBOLS:
            raise ValueError("too many quote symbols")
        return self._request(
            {"operation": "quotes", "symbols": normalized},
            maximum_items=len(normalized),
        )

    def klines(
        self,
        symbol: str,
        period: str,
        start: datetime | None,
        end: datetime | None,
        limit: int,
        adjustment: str = "hfq",
    ) -> list[dict[str, Any]]:
        adjustment = normalize_adjustment(adjustment)
        return self._request(
            {
                "operation": "klines",
                "symbol": normalize_symbol(symbol),
                "period": period,
                "start": start.isoformat() if start is not None else None,
                "end": end.isoformat() if end is not None else None,
                "limit": limit,
                "adjustment": adjustment,
            },
            maximum_items=limit,
        )

    def background_klines(
        self,
        symbol: str,
        period: str,
        start: datetime | None,
        end: datetime | None,
        limit: int,
        adjustment: str = "hfq",
    ) -> list[dict[str, Any]]:
        return self._request(
            {
                "operation": "klines",
                "symbol": normalize_symbol(symbol),
                "period": period,
                "start": start.isoformat() if start is not None else None,
                "end": end.isoformat() if end is not None else None,
                "limit": limit,
                "adjustment": adjustment,
            },
            maximum_items=limit,
            background=True,
        )

    def sessions(self, start_date: date, end_date: date) -> tuple[date, ...]:
        rows = self._request(
            {
                "operation": "sessions",
                "start": start_date.isoformat(),
                "end": end_date.isoformat(),
            },
            maximum_items=MAX_CALENDAR_ITEMS,
        )
        values: set[date] = set()
        for item in rows:
            raw = item.get("date")
            if not isinstance(raw, str):
                continue
            try:
                values.add(date.fromisoformat(raw))
            except ValueError:
                continue
        return tuple(sorted(values))


class SinaMarketProvider:
    """Public Sina endpoints for live A-share quotes and intraday bars.

    The endpoint requires no account or API key.  It is deliberately used only for
    live display data: the market service's short shared cache limits traffic and
    this provider is never used by the frozen research/backtest pipeline.
    """

    source = "sina"
    delayed = False
    _quote_url = "https://hq.sinajs.cn/list="
    _kline_url = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketData.getKLineData"
    _headers: ClassVar[dict[str, str]] = {
        "Referer": "https://finance.sina.com.cn/",
        "User-Agent": "Mozilla/5.0 (compatible; AShareResearch/1.0)",
    }
    _scales: ClassVar[dict[str, str]] = {
        "1": "1",
        "5": "5",
        "15": "15",
        "30": "30",
        "60": "60",
    }

    @staticmethod
    def _provider_symbol(symbol: str) -> str:
        return _sina_provider_symbol(symbol)

    def quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        requested = {
            self._provider_symbol(symbol): normalize_symbol(symbol) for symbol in symbols
        }
        if not requested:
            return []
        response = httpx.get(
            f"{self._quote_url}{','.join(requested)}",
            headers=self._headers,
            timeout=8.0,
        )
        response.raise_for_status()
        payload = response.content.decode("gb18030", errors="replace")
        rows: list[dict[str, Any]] = []
        for match in re.finditer(r'var hq_str_([a-z]{2}\d{6})="([^"]*)";', payload):
            provider_symbol, raw = match.groups()
            symbol = requested.get(provider_symbol)
            fields = raw.split(",")
            if symbol is None or len(fields) < 10 or not fields[0]:
                continue
            previous_close = _number(fields[2])
            price = _number(fields[3])
            change = (
                price - previous_close
                if price is not None and previous_close is not None
                else None
            )
            rows.append(
                {
                    "symbol": symbol,
                    "name": fields[0],
                    "price": price,
                    "change": change,
                    "change_percent": (
                        change / previous_close * 100
                        if change is not None
                        and previous_close is not None
                        and previous_close != 0
                        else None
                    ),
                    "open": _number(fields[1]),
                    "high": _number(fields[4]),
                    "low": _number(fields[5]),
                    "previous_close": previous_close,
                    "volume": _number(fields[8]),
                    "amount": _number(fields[9]),
                    "_trading_at": (
                        f"{fields[30]}T{fields[31]}+08:00" if len(fields) > 31 else None
                    ),
                }
            )
        return rows

    def klines(
        self,
        symbol: str,
        period: str,
        start: datetime | None,
        end: datetime | None,
        limit: int,
        adjustment: str = "hfq",
    ) -> list[dict[str, Any]]:
        scale = self._scales.get(period)
        if scale is None:
            raise ValueError("Sina fallback only supports intraday K-lines")
        response = httpx.get(
            self._kline_url,
            params={
                "symbol": self._provider_symbol(symbol),
                "scale": scale,
                "ma": "no",
                "datalen": str(limit),
            },
            headers=self._headers,
            timeout=8.0,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise RuntimeError("Sina returned an invalid K-line payload")
        lower = _timestamp(start) if start is not None else None
        upper = _timestamp(end) if end is not None else None
        bars: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            timestamp = _timestamp(item.get("day"))
            if (lower is not None and timestamp < lower) or (
                upper is not None and timestamp > upper
            ):
                continue
            open_price = _number(item.get("open"))
            high = _number(item.get("high"))
            low = _number(item.get("low"))
            close = _number(item.get("close"))
            volume = _number(item.get("volume"))
            if None in (open_price, high, low, close, volume):
                continue
            bars.append(
                {
                    "timestamp": timestamp.isoformat(),
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                    "amount": _number(item.get("amount")),
                    "turnover_rate": _number(item.get("turnover_rate")),
                }
            )
        return sorted(bars, key=lambda item: item["timestamp"])[-limit:]


class TencentHfqDailyMarketProvider:
    """Public Tencent endpoint for daily bars with an explicit price basis."""

    source = "tencent"
    delayed = False
    _kline_url = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
    _headers: ClassVar[dict[str, str]] = {
        "Referer": "https://gu.qq.com/",
        "User-Agent": "Mozilla/5.0 (compatible; AShareResearch/1.0)",
    }

    @staticmethod
    def _provider_symbol(symbol: str) -> str:
        return SinaMarketProvider._provider_symbol(symbol)

    def quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        del symbols
        return []

    def klines(
        self,
        symbol: str,
        period: str,
        start: datetime | None,
        end: datetime | None,
        limit: int,
        adjustment: str = "hfq",
    ) -> list[dict[str, Any]]:
        if period != "daily":
            raise ValueError("Tencent fallback only supports daily K-lines")
        adjustment = normalize_adjustment(adjustment)
        effective_end = _timestamp(end or datetime.now(SHANGHAI))
        effective_start = _timestamp(
            start or effective_end - timedelta(days=max(limit * 2, 365))
        )
        provider_symbol = self._provider_symbol(symbol)
        bars_by_timestamp: dict[str, dict[str, Any]] = {}
        for year in range(effective_start.year, effective_end.year + 1):
            variable = f"kline_day{'hfq' if adjustment == 'hfq' else ''}{year}"
            response = httpx.get(
                self._kline_url,
                params={
                    "_var": variable,
                    "param": (
                        f"{provider_symbol},day,{year}-01-01,{year + 1}-12-31,640,{adjustment}"
                    ),
                    "r": "0.8205512681390605",
                },
                headers=self._headers,
                timeout=8.0,
            )
            response.raise_for_status()
            text = response.content.decode("utf-8", errors="replace")
            first, last = text.find("{"), text.rfind("}")
            if first < 0 or last < first:
                raise RuntimeError("Tencent returned an invalid K-line payload")
            payload = json.loads(text[first : last + 1])
            data = payload.get("data", {}).get(provider_symbol, {})
            # Tencent exposes post-adjusted rows as ``hfqday`` for equities,
            # but index symbols (including sh000300) use ``day`` despite the
            # request's ``hfq`` adjustment parameter. Raw requests must use
            # ``day`` only; falling back to ``hfqday`` there would recreate the
            # dividend-induced price mismatch this adapter is meant to prevent.
            rows = (
                data.get("hfqday") or data.get("day", [])
                if adjustment == "hfq"
                else data.get("day", [])
            )
            if not isinstance(rows, list):
                continue
            for item in rows:
                if not isinstance(item, list) or len(item) < 6:
                    continue
                timestamp = _timestamp(item[0])
                if timestamp < effective_start or timestamp > effective_end:
                    continue
                open_price = _number(item[1])
                close = _number(item[2])
                high = _number(item[3])
                low = _number(item[4])
                volume = _number(item[5])
                if None in (open_price, high, low, close, volume):
                    continue
                bars_by_timestamp[timestamp.isoformat()] = {
                    "timestamp": timestamp.isoformat(),
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": volume,
                    "amount": _number(item[8]) if len(item) > 8 else None,
                    "turnover_rate": None,
                }
        return sorted(bars_by_timestamp.values(), key=lambda item: item["timestamp"])[-limit:]


class TushareMarketProvider:
    source = "tushare"

    def __init__(self, token: str) -> None:
        self.sdk = import_module("tushare")
        self.client = self.sdk.pro_api(token)

    def quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for symbol in symbols:
            frame = self.client.daily(
                ts_code=symbol,
                start_date=(date.today() - timedelta(days=7)).strftime("%Y%m%d"),
                end_date=date.today().strftime("%Y%m%d"),
            )
            if frame.empty:
                continue
            item = frame.iloc[0].to_dict()
            rows.append(
                {
                    "symbol": symbol,
                    "name": None,
                    "price": _number(item.get("close")),
                    "change": _number(item.get("change")),
                    "change_percent": _number(item.get("pct_chg")),
                    "open": _number(item.get("open")),
                    "high": _number(item.get("high")),
                    "low": _number(item.get("low")),
                    "previous_close": _number(item.get("pre_close")),
                    "volume": _number(item.get("vol")),
                    "amount": _number(item.get("amount")),
                }
            )
        return rows

    def klines(
        self,
        symbol: str,
        period: str,
        start: datetime | None,
        end: datetime | None,
        limit: int,
        adjustment: str = "hfq",
    ) -> list[dict[str, Any]]:
        adjustment = normalize_adjustment(adjustment)
        effective_end = end or datetime.now(SHANGHAI)
        if period == "daily":
            effective_start = start or effective_end - timedelta(days=max(limit * 2, 365))
            start_value = effective_start.strftime("%Y%m%d")
            end_value = effective_end.strftime("%Y%m%d")
            frequency = "D"
            timestamp_field = "trade_date"
        else:
            effective_start = start or effective_end - timedelta(days=max(5, limit // 20 + 1))
            start_value = effective_start.strftime("%Y-%m-%d %H:%M:%S")
            end_value = effective_end.strftime("%Y-%m-%d %H:%M:%S")
            frequency = f"{period}min"
            timestamp_field = "trade_time"
        frame = self.sdk.pro_bar(
            api=self.client,
            ts_code=normalize_symbol(symbol),
            start_date=start_value,
            end_date=end_value,
            adj="hfq" if adjustment == "hfq" else None,
            freq=frequency,
        )
        if frame is None or frame.empty:
            return []
        result = []
        for item in frame.sort_values(timestamp_field).tail(limit).to_dict(orient="records"):
            result.append(
                {
                    "timestamp": _timestamp(item[timestamp_field]).isoformat(),
                    "open": _number(item.get("open")) or 0.0,
                    "high": _number(item.get("high")) or 0.0,
                    "low": _number(item.get("low")) or 0.0,
                    "close": _number(item.get("close")) or 0.0,
                    "volume": _number(item.get("vol")) or 0.0,
                    "amount": _number(item.get("amount")),
                    "turnover_rate": _number(item.get("turnover_rate")),
                }
            )
        return result


class MarketDataService:
    """Best-effort live data. This service never persists research/backtest snapshots."""

    def __init__(
        self,
        *,
        primary: MarketProvider | None = None,
        fallback: MarketProvider | None = None,
        settings: Settings | None = None,
        clock: Any = None,
        redis_client: Any = ...,
    ) -> None:
        self.settings = settings or get_settings()
        self.runtime_policy = runtime_mode_policy(self.settings)
        profile_limits = primary is None
        self._cache_max_entries = (
            self.runtime_policy.market_cache_max_entries
            if profile_limits
            else self.settings.market_cache_max_entries
        )
        self._prefetch_max_workers = (
            self.runtime_policy.market_prefetch_max_workers
            if profile_limits
            else self.settings.market_prefetch_max_workers
        )
        self._provider_max_workers = (
            self.runtime_policy.market_provider_max_workers
            if profile_limits
            else self.settings.market_provider_max_workers
        )
        self._provider_max_queue = (
            self.runtime_policy.market_provider_max_queue
            if profile_limits
            else self.settings.market_provider_max_queue
        )
        if primary is not None:
            self.primary = primary
        elif self.runtime_policy.use_isolated_akshare:
            self.primary = AKShareMarketProvider(
                timeout_seconds=self.settings.market_timeout_seconds
            )
        else:
            self.primary = SinaMarketProvider()
        if fallback is not None:
            self.fallbacks: tuple[MarketProvider, ...] = (fallback,)
        else:
            defaults: list[MarketProvider] = [TencentHfqDailyMarketProvider()]
            if not isinstance(self.primary, SinaMarketProvider):
                defaults.insert(0, SinaMarketProvider())
            if self.settings.tushare_token:
                defaults.append(TushareMarketProvider(self.settings.tushare_token))
            self.fallbacks = tuple(defaults)
        # Keep the original attribute for callers that only need the first fallback.
        self.fallback = self.fallbacks[0] if self.fallbacks else None
        self.clock = clock or (lambda: datetime.now(UTC))
        self._redis_override = redis_client
        self._cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._cache_guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        # Distinct market cache keys (klines start/end/limit combinations) are
        # directly influenced by authenticated callers.  The cache itself is
        # LRU-bounded, so the per-key lock table is capped at twice that and any
        # overflow keys share one fallback lock instead of growing memory.
        self._max_locks = max(128, self._cache_max_entries * 2)
        self._overflow_lock = threading.Lock()
        self._provider_slots = threading.BoundedSemaphore(
            max(
                self._provider_max_workers,
                self._provider_max_queue,
            )
        )

    def start(self) -> bool:
        if isinstance(self.primary, AKShareMarketProvider):
            if not self._provider_allowed(self.primary):
                return True
            return self.primary.start()
        return True

    def close(self) -> None:
        if isinstance(self.primary, AKShareMarketProvider):
            self.primary.close()
        with self._cache_guard:
            self._cache.clear()
        with self._guard:
            self._locks.clear()

    def release_after_close(self) -> None:
        if self.runtime_policy.auto_close_after_close and is_after_close(self.clock()):
            self.close()

    def _provider_allowed(self, provider: MarketProvider) -> bool:
        return not (
            isinstance(provider, AKShareMarketProvider)
            and self.runtime_policy.auto_close_after_close
            and is_after_close(self.clock())
        )

    def sessions(self, start_date: date, end_date: date) -> tuple[date, ...]:
        # The whole calendar is cached, so a submit's 20-day lookup, the
        # scheduler's 20-day window and a worker's 14-day retry window all hit
        # the same record instead of each fetching AKShare.
        now = self.clock()
        cached = self._get(_CALENDAR_CACHE_KEY, now)
        if cached is not None and self._fresh(cached, now):
            return _calendar_range(cached.get("sessions"), start_date, end_date)
        with self._lock(_CALENDAR_CACHE_KEY):
            now = self.clock()
            cached = self._get(_CALENDAR_CACHE_KEY, now)
            if cached is not None and self._fresh(cached, now):
                return _calendar_range(cached.get("sessions"), start_date, end_date)
            value = self._fetch_calendar()
            if value:
                self._set(
                    _CALENDAR_CACHE_KEY,
                    {
                        "sessions": [day.isoformat() for day in value],
                        "cached_at": self.clock().isoformat(),
                        "cache_seconds": _CALENDAR_CACHE_SECONDS,
                    },
                )
            return _calendar_range([day.isoformat() for day in value], start_date, end_date)

    def _fetch_calendar(self) -> tuple[date, ...]:
        provider_sessions = getattr(self.primary, "sessions", None)
        if callable(provider_sessions) and self._provider_allowed(self.primary):
            try:
                return tuple(provider_sessions(_CALENDAR_FULL_START, _CALENDAR_FULL_END))
            except Exception:
                # A failed AKShare request can leave the reusable child alive but
                # tied to a broken upstream connection. Recreate it once below.
                if isinstance(self.primary, AKShareMarketProvider):
                    self.primary.close()
        # Calendar reads are rare and must not make the lightweight API retain
        # the AKShare runtime after the request completes.
        isolated = AKShareMarketProvider(timeout_seconds=self.settings.market_timeout_seconds)
        try:
            return tuple(isolated.sessions(_CALENDAR_FULL_START, _CALENDAR_FULL_END))
        finally:
            isolated.close()

    def _lock(self, key: str) -> threading.Lock:
        with self._guard:
            lock = self._locks.get(key)
            if lock is not None:
                return lock
            if len(self._locks) >= self._max_locks:
                # Bounded memory under cache-key abuse: overflow keys share one
                # stable lock.  Mutual exclusion still holds (the same key always
                # resolves to the same object) and only abuse-generated keys pay
                # the extra contention.
                return self._overflow_lock
            lock = threading.Lock()
            self._locks[key] = lock
            return lock

    def _redis(self) -> Any | None:
        if self._redis_override is not ...:
            return self._redis_override
        try:
            import redis

            return redis.Redis.from_url(
                self.settings.redis_url, decode_responses=True, socket_timeout=1
            )
        except Exception:
            return None

    def _get(self, key: str, now: datetime | None = None) -> dict[str, Any] | None:
        with self._cache_guard:
            local = self._cache.get(key)
            if local is not None and (now is None or self._fresh(local, now)):
                self._cache.move_to_end(key)
                return local
        client = self._redis()
        if client is None:
            return local
        try:
            payload = client.get(f"ashare:market:{key}")
            if payload:
                value = cast(dict[str, Any], json.loads(payload))
                if local is None or value.get("cached_at", "") >= local.get("cached_at", ""):
                    self._cache_set(key, value)
                    return value
        except Exception:
            pass
        return local

    def _get_shared(self, key: str) -> dict[str, Any] | None:
        client = self._redis()
        if client is None:
            return None
        try:
            payload = client.get(f"ashare:market:{key}")
            if payload:
                value = cast(dict[str, Any], json.loads(payload))
                self._cache_set(key, value)
                return value
        except Exception:
            pass
        return None

    def _claim_refresh(self, key: str) -> tuple[Any | None, str | None, bool]:
        client = self._redis()
        if client is None:
            return None, None, True
        token = secrets.token_urlsafe(16)
        try:
            claimed = bool(
                client.set(
                    f"ashare:market:refresh:{key}",
                    token,
                    nx=True,
                    ex=max(2, int(self.settings.market_timeout_seconds) + 2),
                )
            )
            return client, token, claimed
        except Exception:
            return None, None, True

    @staticmethod
    def _release_refresh(client: Any | None, key: str, token: str | None) -> None:
        if client is None or token is None:
            return
        script = (
            "if redis.call('get', KEYS[1]) == ARGV[1] then "
            "return redis.call('del', KEYS[1]) else return 0 end"
        )
        with suppress(Exception):
            client.eval(script, 1, f"ashare:market:refresh:{key}", token)

    def _set(self, key: str, value: dict[str, Any]) -> None:
        self._cache_set(key, value)
        client = self._redis()
        if client is not None:
            with suppress(Exception):
                client.setex(
                    f"ashare:market:{key}",
                    self.settings.market_stale_seconds,
                    json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                )

    def _cache_set(self, key: str, value: dict[str, Any]) -> None:
        with self._cache_guard:
            self._cache[key] = value
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_max_entries:
                self._cache.popitem(last=False)

    @staticmethod
    def _normalize_quote(item: dict[str, Any]) -> dict[str, Any]:
        """Quotes are provider-native, unadjusted prices by contract."""

        return {**item, "price_basis": "raw"}

    def _submit_call(self, function: Any, *args: Any) -> Future[Any]:
        if not self._provider_slots.acquire(blocking=False):
            raise TimeoutError("market provider queue is saturated")
        try:
            future = _PROVIDER_EXECUTOR.submit(function, *args)
        except Exception:
            self._provider_slots.release()
            raise
        future.add_done_callback(lambda _: self._provider_slots.release())
        return future

    def _call(self, function: Any, *args: Any) -> Any:
        """Use a shared bounded pool and retain timed-out slots until completion.

        Cancelling a Python thread cannot stop an already running provider call. A
        semaphore therefore stays acquired until its future has actually finished,
        bounding both concurrent calls and any residual work after a timeout.
        """

        future = self._submit_call(function, *args)
        try:
            return future.result(timeout=self.settings.market_timeout_seconds)
        except FutureTimeoutError as exc:
            future.cancel()
            raise TimeoutError("market provider timeout") from exc

    def _call_kline_provider(
        self,
        function: Any,
        symbol: str,
        period: str,
        start: datetime | None,
        end: datetime | None,
        limit: int,
        adjustment: str,
    ) -> Any:
        """Call legacy test/custom providers while passing the basis to built-ins."""

        try:
            parameters = signature(function).parameters.values()
            supports_adjustment = any(
                parameter.name == "adjustment"
                or parameter.kind == Parameter.VAR_POSITIONAL
                or parameter.kind == Parameter.VAR_KEYWORD
                for parameter in parameters
            )
        except (TypeError, ValueError):
            supports_adjustment = True
        args = (symbol, period, start, end, limit)
        if supports_adjustment:
            return self._call(function, *args, adjustment)
        return self._call(function, *args)

    def _hedged_daily_kline(
        self,
        tencent: TencentHfqDailyMarketProvider,
        symbol: str,
        start: datetime | None,
        end: datetime | None,
        limit: int,
        *,
        background: bool,
        adjustment: str,
    ) -> tuple[MarketProvider, list[dict[str, Any]], datetime]:
        """Start Tencent only when the warmed AKShare request misses its latency budget."""

        primary_call = (
            self.primary.background_klines
            if background and isinstance(self.primary, AKShareMarketProvider)
            else self.primary.klines
        )
        started = time.monotonic()
        deadline = started + self.settings.market_timeout_seconds
        primary_collected = self.clock()
        primary_future = self._submit_call(
            primary_call, symbol, "daily", start, end, limit, adjustment
        )
        futures: dict[Future[Any], tuple[MarketProvider, datetime]] = {
            primary_future: (self.primary, primary_collected)
        }
        stale: list[tuple[MarketProvider, list[dict[str, Any]], datetime]] = []
        errors: list[str] = []
        try:
            bars = _validated_kline_bars(
                primary_future.result(timeout=self.settings.market_hedge_delay_seconds),
                "daily",
                start,
                end,
                limit,
            )
            if not _intraday_series_is_stale(bars, "daily", end, primary_collected):
                return self.primary, bars, primary_collected
            stale.append((self.primary, bars, primary_collected))
        except FutureTimeoutError:
            pass
        except Exception as exc:
            errors.append(f"{self.primary.source}: {exc}")

        if not primary_future.done():
            futures[primary_future] = (self.primary, primary_collected)
        else:
            futures.pop(primary_future, None)
        tencent_collected = self.clock()
        try:
            tencent_future = self._submit_call(
                tencent.klines, symbol, "daily", start, end, limit, adjustment
            )
            futures[tencent_future] = (tencent, tencent_collected)
        except Exception as exc:
            errors.append(f"{tencent.source}: {exc}")
        while futures:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            completed, _ = wait(
                tuple(futures), timeout=remaining, return_when=FIRST_COMPLETED
            )
            if not completed:
                break
            for future in completed:
                provider, collected = futures.pop(future)
                try:
                    bars = _validated_kline_bars(
                        future.result(), "daily", start, end, limit
                    )
                    if not _intraday_series_is_stale(
                        bars, "daily", end, collected
                    ):
                        return provider, bars, collected
                    stale.append((provider, bars, collected))
                except Exception as exc:
                    errors.append(f"{provider.source}: {exc}")
        if stale:
            return max(stale, key=lambda item: _timestamp(item[1][-1]["timestamp"]))
        raise RuntimeError("; ".join(errors) or "daily providers timed out")

    def _status(
        self,
        source: str,
        collected_at: str,
        cached_at: str,
        *,
        delayed: bool,
        message: str | None = None,
    ) -> dict[str, Any]:
        return {
            "source": source,
            "collected_at": collected_at,
            "cached_at": cached_at,
            "delayed": delayed,
            "stale": delayed,
            "message": message,
        }

    def _fresh(self, record: dict[str, Any], now: datetime) -> bool:
        cached_at = datetime.fromisoformat(record["cached_at"])
        cache_seconds = int(record.get("cache_seconds", self.settings.market_cache_seconds))
        return (now - cached_at).total_seconds() < cache_seconds

    def _usable_stale(self, record: dict[str, Any], now: datetime) -> bool:
        cached_at = datetime.fromisoformat(record["cached_at"])
        return (now - cached_at).total_seconds() <= self.settings.market_stale_seconds

    def quotes(
        self, symbols: list[str], *, force_refresh: bool = False
    ) -> list[dict[str, Any]]:
        normalized = sorted(set(normalize_symbol(item) for item in symbols if item.strip()))
        if not normalized:
            return []
        # AKShare's spot endpoint returns one full-market snapshot. A global key and lock
        # therefore coalesce disjoint page/watchlist/holding requests into one upstream call.
        key = "quotes:all"

        def selected(
            record: dict[str, Any], *, cache_hit: bool = False
        ) -> list[dict[str, Any]]:
            requested = set(normalized)
            rows: list[dict[str, Any]] = []
            for item in record["items"]:
                if item["symbol"] not in requested:
                    continue
                status = item.get("status") if isinstance(item.get("status"), dict) else {}
                rows.append(
                    {
                        **self._normalize_quote(item),
                        "status": {**status, "cache_hit": cache_hit},
                    }
                )
            return rows

        now = self.clock()
        cached = self._get(key, now)
        if (
            not force_refresh
            and cached is not None
            and self._fresh(cached, now)
            and len(selected(cached)) == len(normalized)
        ):
            return selected(cached, cache_hit=True)
        with self._lock(key):
            now = self.clock()
            cached = self._get(key, now)
            if (
                not force_refresh
                and cached is not None
                and self._fresh(cached, now)
                and len(selected(cached)) == len(normalized)
            ):
                return selected(cached, cache_hit=True)
            redis_client, refresh_token, claimed = self._claim_refresh(key)
            if not claimed:
                deadline = time.monotonic() + self.settings.market_timeout_seconds
                while time.monotonic() < deadline:
                    shared = self._get_shared(key)
                    if (
                        shared is not None
                        and self._fresh(shared, self.clock())
                        and len(selected(shared)) == len(normalized)
                    ):
                        return selected(shared, cache_hit=True)
                    time.sleep(0.05)
            errors: list[str] = []
            try:
                usable_cached = (
                    cached
                    if cached is not None and self._usable_stale(cached, now)
                    else None
                )
                cached_by_symbol = {
                    item["symbol"]: item for item in (usable_cached or {}).get("items", [])
                }
                refreshed: dict[str, dict[str, Any]] = {}
                missing = set(normalized)
                for provider in (self.primary, *self.fallbacks):
                    if not self._provider_allowed(provider):
                        continue
                    if not missing:
                        break
                    try:
                        collected = self.clock()
                        provider_items = self._call(provider.quotes, sorted(missing))
                        cached_at = self.clock()
                        provider_status = self._status(
                            provider.source,
                            collected.isoformat(),
                            cached_at.isoformat(),
                            delayed=(
                                provider is not self.primary
                                and getattr(provider, "delayed", True)
                            ),
                        )
                        returned: set[str] = set()
                        for item in provider_items:
                            item_symbol = item.get("symbol")
                            if not isinstance(item_symbol, str):
                                continue
                            refreshed[item_symbol] = {
                                **self._normalize_quote(item),
                                "status": provider_status,
                            }
                            returned.add(item_symbol)
                        missing -= returned
                    except Exception as exc:
                        errors.append(f"{provider.source}: {exc}")
                merged = {**cached_by_symbol, **refreshed}
                unresolved = set(normalized) - set(merged)
                if unresolved:
                    raise RuntimeError(
                        f"market data missing requested symbols: {sorted(unresolved)}"
                    )
                if missing:
                    message = "; ".join(errors) or "upstream unavailable"
                    for symbol in missing:
                        if symbol in merged:
                            merged[symbol] = {
                                **merged[symbol],
                                "status": {
                                    **merged[symbol]["status"],
                                    "delayed": True,
                                    "stale": True,
                                    "message": message,
                                },
                            }
                record = {
                    "cached_at": (
                        cached["cached_at"]
                        if missing and cached is not None
                        else cached_at.isoformat()
                    ),
                    "cache_seconds": self.settings.market_cache_seconds,
                    "items": [merged[symbol] for symbol in sorted(merged)],
                }
                self._set(key, record)
                return selected(record, cache_hit=False)
            finally:
                if claimed:
                    self._release_refresh(redis_client, key, refresh_token)

    def quote(self, symbol: str, *, force_refresh: bool = False) -> dict[str, Any]:
        """Fetch one display quote through the low-latency provider before full snapshots.

        The regular bulk endpoint intentionally coalesces requests through AKShare's
        all-market snapshot.  That is efficient for background work but makes a
        user's currently selected security wait for unrelated symbols.  A separate
        per-symbol cache therefore uses the targeted fallback path (Sina by
        default) first and only falls back to the full-market source when needed.
        """

        normalized = normalize_symbol(symbol)
        key = f"quote:{normalized}"
        now = self.clock()
        cached = self._get(key, now)

        def cached_item(record: dict[str, Any], *, cache_hit: bool = False) -> dict[str, Any]:
            item = self._normalize_quote(dict(record["item"]))
            status: dict[str, Any] = (
                dict(item["status"]) if isinstance(item.get("status"), dict) else {}
            )
            return {**item, "status": {**status, "cache_hit": cache_hit}}

        if not force_refresh and cached is not None and self._fresh(cached, now):
            return cached_item(cached, cache_hit=True)
        with self._lock(key):
            now = self.clock()
            cached = self._get(key, now)
            if not force_refresh and cached is not None and self._fresh(cached, now):
                return cached_item(cached, cache_hit=True)
            redis_client, refresh_token, claimed = self._claim_refresh(key)
            if not claimed:
                deadline = time.monotonic() + self.settings.market_timeout_seconds
                while time.monotonic() < deadline:
                    shared = self._get_shared(key)
                    if shared is not None and self._fresh(shared, self.clock()):
                        return cached_item(shared, cache_hit=True)
                    time.sleep(0.05)
            errors: list[str] = []
            try:
                for provider in (*self.fallbacks, self.primary):
                    if not self._provider_allowed(provider):
                        continue
                    try:
                        collected = self.clock()
                        rows = self._call(provider.quotes, [normalized])
                        item = next(
                            (
                                row
                                for row in rows
                                if isinstance(row.get("symbol"), str)
                                and row["symbol"] == normalized
                            ),
                            None,
                        )
                        if item is None:
                            raise RuntimeError("provider returned no requested quote")
                        cached_at = self.clock()
                        value = {
                            **self._normalize_quote(item),
                            "status": self._status(
                                provider.source,
                                collected.isoformat(),
                                cached_at.isoformat(),
                                delayed=(
                                    provider is not self.primary
                                    and getattr(provider, "delayed", True)
                                ),
                            ),
                        }
                        record = {
                            "cached_at": cached_at.isoformat(),
                            "cache_seconds": self.settings.market_cache_seconds,
                            "item": value,
                        }
                        self._set(key, record)
                        return cached_item(record)
                    except Exception as exc:
                        errors.append(f"{provider.source}: {exc}")
                if cached is not None and self._usable_stale(cached, now):
                    stale = cached_item(cached)
                    status = dict(stale["status"])
                    stale["status"] = {
                        **status,
                        "delayed": True,
                        "stale": True,
                        "message": "; ".join(errors) or "upstream unavailable",
                    }
                    return stale
                raise RuntimeError("market quote unavailable")
            finally:
                if claimed:
                    self._release_refresh(redis_client, key, refresh_token)

    def klines(
        self,
        symbol: str,
        period: str,
        *,
        limit: int = 300,
        start: datetime | None = None,
        end: datetime | None = None,
        force_refresh: bool = False,
        background: bool = False,
        adjustment: str = "hfq",
    ) -> dict[str, Any]:
        normalized = normalize_symbol(symbol)
        adjustment = normalize_adjustment(adjustment)
        provider_period = PERIODS.get(period.casefold())
        if provider_period is None:
            raise ValueError(f"unsupported period: {period}")
        key = _kline_cache_key(
            normalized,
            provider_period,
            limit,
            start,
            end,
            bucket_seconds=self.settings.market_cache_seconds,
            adjustment=adjustment,
        )
        fetch_start, fetch_end = _canonical_kline_bounds(
            provider_period,
            start,
            end,
            bucket_seconds=self.settings.market_cache_seconds,
        )
        now = self.clock()
        cached = self._get(key, now)
        if not force_refresh and cached is not None and self._fresh(cached, now):
            return _mark_kline_cache_hit(
                _filter_kline_range(cast(dict[str, Any], cached["value"]), start, end, limit)
            )
        with self._lock(key):
            now = self.clock()
            cached = self._get(key, now)
            if not force_refresh and cached is not None and self._fresh(cached, now):
                return _mark_kline_cache_hit(
                    _filter_kline_range(
                        cast(dict[str, Any], cached["value"]), start, end, limit
                    )
                )
            redis_client, refresh_token, claimed = self._claim_refresh(key)
            if not claimed:
                deadline = time.monotonic() + self.settings.market_timeout_seconds
                while time.monotonic() < deadline:
                    shared = self._get_shared(key)
                    if shared is not None and self._fresh(shared, self.clock()):
                        return _mark_kline_cache_hit(
                            _filter_kline_range(
                                cast(dict[str, Any], shared["value"]), start, end, limit
                            )
                        )
                    time.sleep(0.05)
            errors: list[str] = []
            stale_primary_bars: list[dict[str, Any]] | None = None
            stale_candidate: tuple[MarketProvider, list[dict[str, Any]]] | None = None
            try:
                preloaded: tuple[MarketProvider, list[dict[str, Any]], datetime] | None = None
                providers_to_call: list[MarketProvider] = [
                    provider
                    for provider in (self.primary, *self.fallbacks)
                    if self._provider_allowed(provider)
                ]
                tencent = next(
                    (
                        provider
                        for provider in self.fallbacks
                        if isinstance(provider, TencentHfqDailyMarketProvider)
                    ),
                    None,
                )
                if (
                    provider_period == "daily"
                    and isinstance(self.primary, AKShareMarketProvider)
                    and self._provider_allowed(self.primary)
                    and tencent is not None
                ):
                    try:
                        preloaded = self._hedged_daily_kline(
                            tencent,
                            normalized,
                            fetch_start,
                            fetch_end,
                            limit,
                            background=background,
                            adjustment=adjustment,
                        )
                    except Exception as exc:
                        errors.append(f"daily hedge: {exc}")
                    providers_to_call = [
                        provider
                        for provider in self.fallbacks
                        if provider is not tencent
                    ]
                attempts = ([preloaded[0]] if preloaded is not None else []) + providers_to_call
                for provider in attempts:
                    try:
                        if preloaded is not None and provider is preloaded[0]:
                            _, bars, collected = preloaded
                            preloaded = None
                        else:
                            collected = self.clock()
                            provider_call = (
                                provider.background_klines
                                if background and isinstance(provider, AKShareMarketProvider)
                                else provider.klines
                            )
                            bars = self._call_kline_provider(
                                provider_call,
                                normalized,
                                provider_period,
                                fetch_start,
                                fetch_end,
                                limit,
                                adjustment,
                            )
                            bars = _validated_kline_bars(
                                bars, provider_period, fetch_start, fetch_end, limit
                            )
                        if not bars:
                            raise RuntimeError("provider returned no bars")
                        source = provider.source
                        status_message = None
                        if _intraday_series_is_stale(bars, provider_period, end, collected):
                            if stale_candidate is None or _timestamp(
                                bars[-1]["timestamp"]
                            ) > _timestamp(stale_candidate[1][-1]["timestamp"]):
                                stale_candidate = (provider, bars)
                            if provider is self.primary:
                                stale_primary_bars = bars
                            raise RuntimeError("provider returned a stale K-line series")
                        if stale_primary_bars is not None and provider_period != "daily":
                            merged = _merge_adjusted_intraday(
                                stale_primary_bars,
                                bars,
                                limit,
                                adjustment=adjustment,
                            )
                            if merged is None:
                                raise RuntimeError(
                                    f"fallback cannot be aligned to the {adjustment} series"
                                )
                            bars = merged
                            source = f"{self.primary.source}+{provider.source}"
                            status_message = "主数据源分钟线滞后，已用实时备用源对齐补齐"
                        cached_at = self.clock()
                        value = {
                            "symbol": normalized,
                            "period": (
                                "day" if provider_period == "daily" else f"{provider_period}m"
                            ),
                            "adjustment": adjustment,
                            "bars": bars,
                            "status": self._status(
                                source,
                                collected.isoformat(),
                                cached_at.isoformat(),
                                delayed=(
                                    provider is not self.primary
                                    and getattr(provider, "delayed", True)
                                ),
                                message=status_message,
                            ),
                        }
                        cache_seconds = (
                            self.settings.market_kline_cache_seconds
                            if provider_period == "daily"
                            else self.settings.market_cache_seconds
                        )
                        self._set(
                            key,
                            {
                                "cached_at": cached_at.isoformat(),
                                "cache_seconds": cache_seconds,
                                "value": value,
                            },
                        )
                        return _filter_kline_range(value, start, end, limit)
                    except Exception as exc:
                        errors.append(f"{provider.source}: {exc}")
                if provider_period == "daily" and stale_candidate is not None:
                    quote_provider = next(
                        (
                            provider
                            for provider in self.fallbacks
                            if isinstance(provider, SinaMarketProvider)
                        ),
                        None,
                    )
                    if quote_provider is not None:
                        try:
                            quote_rows = self._call(quote_provider.quotes, [normalized])
                            merged = _append_adjusted_daily_quote(
                                stale_candidate[1],
                                quote_rows[0],
                                self.clock(),
                                limit,
                                adjustment=adjustment,
                            )
                            if merged is not None:
                                cached_at = self.clock()
                                value = {
                                    "symbol": normalized,
                                    "period": "day",
                                    "adjustment": adjustment,
                                    "bars": merged,
                                    "status": self._status(
                                        f"{stale_candidate[0].source}+{quote_provider.source}",
                                        cached_at.isoformat(),
                                        cached_at.isoformat(),
                                        delayed=False,
                                        message=(
                                            "历史日线滞后，已用当日未复权行情对齐补齐"
                                            if adjustment == "raw"
                                            else "历史后复权日线滞后，已用当日行情对齐补齐"
                                        ),
                                    ),
                                }
                                self._set(
                                    key,
                                    {
                                        "cached_at": cached_at.isoformat(),
                                        "cache_seconds": self.settings.market_kline_cache_seconds,
                                        "value": value,
                                    },
                                )
                                return _filter_kline_range(value, start, end, limit)
                        except Exception as exc:
                            errors.append(f"daily close alignment: {exc}")
                stale_bars = stale_primary_bars or (
                    stale_candidate[1] if stale_candidate is not None else None
                )
                stale_source = (
                    self.primary.source
                    if stale_primary_bars is not None
                    else stale_candidate[0].source
                    if stale_candidate is not None
                    else self.primary.source
                )
                if stale_bars is not None:
                    fallback_at = self.clock()
                    value = {
                        "symbol": normalized,
                        "period": "day" if provider_period == "daily" else f"{provider_period}m",
                        "adjustment": adjustment,
                        "bars": stale_bars,
                        "status": self._status(
                            stale_source,
                            fallback_at.isoformat(),
                            fallback_at.isoformat(),
                            delayed=True,
                            message="K 线最新数据停留在上一交易日，实时备用源不可用",
                        ),
                    }
                    return _filter_kline_range(value, start, end, limit)
                if cached is not None and self._usable_stale(cached, now):
                    value = dict(cached["value"])
                    raw_status = value.get("status")
                    cached_status = dict(raw_status) if isinstance(raw_status, dict) else {}
                    value["status"] = {
                        **cached_status,
                        "delayed": True,
                        "stale": True,
                        "message": "; ".join(errors),
                    }
                    return _filter_kline_range(value, start, end, limit)
                raise RuntimeError("; ".join(errors) or "market data unavailable")
            finally:
                if claimed:
                    self._release_refresh(redis_client, key, refresh_token)

    def prefetch(
        self,
        symbols: list[str],
        *,
        periods: list[str] | None = None,
        limit: int = 160,
        include_quotes: bool = True,
        adjustment: str = "raw",
    ) -> dict[str, Any]:
        """Warm selected live-data caches without coupling them to PIT snapshots."""

        normalized = sorted(set(normalize_symbol(item) for item in symbols if item.strip()))
        if not normalized:
            raise ValueError("at least one symbol is required")
        adjustment = normalize_adjustment(adjustment)
        if len(normalized) > MAX_PREFETCH_SYMBOLS:
            raise ValueError(f"prefetch supports at most {MAX_PREFETCH_SYMBOLS} symbols")
        requested_periods = []
        for period in periods or ["day"]:
            normalized_period = period.casefold()
            if normalized_period not in {"day", "daily"}:
                raise ValueError(f"unsupported prefetch period: {period}")
            if "day" not in requested_periods:
                requested_periods.append("day")
        if not requested_periods:
            raise ValueError("at least one period is required")

        quotes: list[dict[str, Any]] = []
        klines: dict[str, dict[str, dict[str, Any]]] = {}
        errors: dict[str, str] = {}
        task_count = len(normalized) * len(requested_periods) + int(include_quotes)
        max_workers = min(self._prefetch_max_workers, task_count)
        with ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="market-prefetch",
        ) as pool:
            quote_future = pool.submit(self.quotes, normalized) if include_quotes else None
            kline_futures = {
                pool.submit(
                    self.klines,
                    symbol,
                    period,
                    limit=limit,
                    background=True,
                    adjustment=adjustment,
                ): (symbol, period)
                for symbol in normalized
                for period in requested_periods
            }
            if quote_future is not None:
                try:
                    quotes = quote_future.result()
                except Exception as exc:
                    errors["quotes"] = str(exc)
            for future, (symbol, period) in kline_futures.items():
                try:
                    value = future.result()
                    klines.setdefault(symbol, {})[period] = value
                except Exception as exc:
                    errors[symbol if len(requested_periods) == 1 else f"{symbol}:{period}"] = (
                        str(exc)
                    )
        return {
            "quotes": quotes,
            "klines": klines,
            "errors": errors,
        }

    def status(self) -> dict[str, Any]:
        quote_record = self._get("quotes:all")
        quote_status = None
        if quote_record and quote_record.get("items"):
            quote_status = {
                **quote_record["items"][0]["status"],
                "symbols": len(quote_record["items"]),
            }
        return {
            "runtime_mode": self.runtime_policy.mode,
            "runtime_mode_policy": self.runtime_policy.as_dict(),
            "primary": self.primary.source,
            "fallback": self.fallback.source if self.fallback else None,
            "fallbacks": [provider.source for provider in self.fallbacks],
            "cache_seconds": self.settings.market_cache_seconds,
            "kline_cache_seconds": self.settings.market_kline_cache_seconds,
            "prefetch_max_workers": self.settings.market_prefetch_max_workers,
            "prefetch_max_symbols": MAX_PREFETCH_SYMBOLS,
            "stale_seconds": self.settings.market_stale_seconds,
            "adjustment": "hfq",
            "live_quote_price_basis": "raw",
            "live_kline_default_adjustment": "raw",
            "supported_adjustments": ["raw", "hfq"],
            "live_data_isolated_from_snapshots": True,
            "provider_process_mode": (
                "REUSABLE" if isinstance(self.primary, AKShareMarketProvider) else "IN_PROCESS"
            ),
            "provider_process_state": (
                self.primary.state
                if isinstance(self.primary, AKShareMarketProvider)
                else "READY"
            ),
            "provider_process_degraded": (
                self.primary.last_error is not None
                if isinstance(self.primary, AKShareMarketProvider)
                else False
            ),
            "hedge_delay_seconds": self.settings.market_hedge_delay_seconds,
            "quotes": quote_status,
        }


_ACTIVE_MARKET_SERVICE: MarketDataService | None = None


@lru_cache(maxsize=1)
def get_market_data_service() -> MarketDataService:
    global _ACTIVE_MARKET_SERVICE
    service = MarketDataService(settings=get_effective_settings())
    _ACTIVE_MARKET_SERVICE = service
    return service


def reset_market_data_service() -> None:
    global _ACTIVE_MARKET_SERVICE
    if _ACTIVE_MARKET_SERVICE is not None:
        _ACTIVE_MARKET_SERVICE.close()
    _ACTIVE_MARKET_SERVICE = None
    get_market_data_service.cache_clear()
