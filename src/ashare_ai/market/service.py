from __future__ import annotations

import json
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from importlib import import_module
from typing import Any, ClassVar, Protocol, cast

import httpx

from ashare_ai.adapters.symbols import normalize_symbol as canonical_symbol
from ashare_ai.core.config import Settings, get_settings
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
MAX_PREFETCH_SYMBOLS = 50


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
    ) -> list[dict[str, Any]]: ...


def normalize_symbol(value: str) -> str:
    return str(canonical_symbol(value))


def _number(value: Any) -> float | None:
    if value is None or value in {"", "-", "--"}:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


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


class AKShareMarketProvider:
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
    ) -> list[dict[str, Any]]:
        code = normalize_symbol(symbol).split(".", 1)[0]
        sdk = self._sdk()
        effective_end = end or datetime.now(SHANGHAI)
        if period == "daily":
            effective_start = start or effective_end - timedelta(days=max(limit * 2, 365))
            frame = sdk.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=effective_start.strftime("%Y%m%d"),
                end_date=effective_end.strftime("%Y%m%d"),
                adjust="hfq",
            )
        else:
            effective_start = start or effective_end - timedelta(days=max(5, limit // 20 + 1))
            frame = sdk.stock_zh_a_hist_min_em(
                symbol=code,
                period=period,
                start_date=effective_start.strftime("%Y-%m-%d %H:%M:%S"),
                end_date=effective_end.strftime("%Y-%m-%d %H:%M:%S"),
                adjust="hfq",
            )
        bars = []
        for item in frame.tail(limit).to_dict(orient="records"):
            bars.append(
                {
                    "timestamp": _timestamp(item.get("时间", item.get("日期"))).isoformat(),
                    "open": _number(item.get("开盘")) or 0.0,
                    "high": _number(item.get("最高")) or 0.0,
                    "low": _number(item.get("最低")) or 0.0,
                    "close": _number(item.get("收盘")) or 0.0,
                    "volume": _number(item.get("成交量")) or 0.0,
                    "amount": _number(item.get("成交额")),
                    "turnover_rate": _number(item.get("换手率")),
                }
            )
        return bars


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
        code, exchange = normalize_symbol(symbol).split(".", 1)
        prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(exchange)
        if prefix is None:
            raise ValueError(f"unsupported Sina exchange: {exchange}")
        return f"{prefix}{code}"

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
    """Public Tencent endpoint for post-adjusted daily bars without AKShare."""

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
    ) -> list[dict[str, Any]]:
        if period != "daily":
            raise ValueError("Tencent fallback only supports daily K-lines")
        effective_end = _timestamp(end or datetime.now(SHANGHAI))
        effective_start = _timestamp(
            start or effective_end - timedelta(days=max(limit * 2, 365))
        )
        provider_symbol = self._provider_symbol(symbol)
        bars_by_timestamp: dict[str, dict[str, Any]] = {}
        for year in range(effective_start.year, effective_end.year + 1):
            variable = f"kline_dayhfq{year}"
            response = httpx.get(
                self._kline_url,
                params={
                    "_var": variable,
                    "param": (
                        f"{provider_symbol},day,{year}-01-01,{year + 1}-12-31,640,hfq"
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
            rows = data.get("hfqday", [])
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
    ) -> list[dict[str, Any]]:
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
            adj="hfq",
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
        self.primary = primary or AKShareMarketProvider()
        if fallback is not None:
            self.fallbacks: tuple[MarketProvider, ...] = (fallback,)
        else:
            defaults: list[MarketProvider] = [
                SinaMarketProvider(),
                TencentHfqDailyMarketProvider(),
            ]
            if self.settings.tushare_token:
                defaults.append(TushareMarketProvider(self.settings.tushare_token))
            self.fallbacks = tuple(defaults)
        # Keep the original attribute for callers that only need the first fallback.
        self.fallback = self.fallbacks[0] if self.fallbacks else None
        self.clock = clock or (lambda: datetime.now(UTC))
        self._redis_override = redis_client
        self._cache: dict[str, dict[str, Any]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _lock(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

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
        local = self._cache.get(key)
        if local is not None and (now is None or self._fresh(local, now)):
            return local
        client = self._redis()
        if client is None:
            return local
        try:
            payload = client.get(f"ashare:market:{key}")
            if payload:
                value = cast(dict[str, Any], json.loads(payload))
                if local is None or value.get("cached_at", "") >= local.get("cached_at", ""):
                    self._cache[key] = value
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
                self._cache[key] = value
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
        self._cache[key] = value
        client = self._redis()
        if client is not None:
            with suppress(Exception):
                client.setex(
                    f"ashare:market:{key}",
                    self.settings.market_stale_seconds,
                    json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                )

    def _call(self, function: Any, *args: Any) -> Any:
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="market-provider")
        future = pool.submit(function, *args)
        try:
            return future.result(timeout=self.settings.market_timeout_seconds)
        except FutureTimeoutError as exc:
            future.cancel()
            raise TimeoutError("market provider timeout") from exc
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

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

        def selected(record: dict[str, Any]) -> list[dict[str, Any]]:
            requested = set(normalized)
            return [item for item in record["items"] if item["symbol"] in requested]

        now = self.clock()
        cached = self._get(key, now)
        if (
            not force_refresh
            and cached is not None
            and self._fresh(cached, now)
            and len(selected(cached)) == len(normalized)
        ):
            return selected(cached)
        with self._lock(key):
            now = self.clock()
            cached = self._get(key, now)
            if (
                not force_refresh
                and cached is not None
                and self._fresh(cached, now)
                and len(selected(cached)) == len(normalized)
            ):
                return selected(cached)
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
                        return selected(shared)
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
                            refreshed[item_symbol] = {**item, "status": provider_status}
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
                return selected(record)
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
    ) -> dict[str, Any]:
        normalized = normalize_symbol(symbol)
        provider_period = PERIODS.get(period.casefold())
        if provider_period is None:
            raise ValueError(f"unsupported period: {period}")
        key = f"klines:{normalized}:{provider_period}:{limit}:{start}:{end}:hfq"
        now = self.clock()
        cached = self._get(key, now)
        if not force_refresh and cached is not None and self._fresh(cached, now):
            return cast(dict[str, Any], cached["value"])
        with self._lock(key):
            now = self.clock()
            cached = self._get(key, now)
            if not force_refresh and cached is not None and self._fresh(cached, now):
                return cast(dict[str, Any], cached["value"])
            redis_client, refresh_token, claimed = self._claim_refresh(key)
            if not claimed:
                deadline = time.monotonic() + self.settings.market_timeout_seconds
                while time.monotonic() < deadline:
                    shared = self._get_shared(key)
                    if shared is not None and self._fresh(shared, self.clock()):
                        return cast(dict[str, Any], shared["value"])
                    time.sleep(0.05)
            errors: list[str] = []
            try:
                for provider in (self.primary, *self.fallbacks):
                    try:
                        collected = self.clock()
                        bars = self._call(
                            provider.klines, normalized, provider_period, start, end, limit
                        )
                        if not bars:
                            raise RuntimeError("provider returned no bars")
                        cached_at = self.clock()
                        value = {
                            "symbol": normalized,
                            "period": (
                                "day" if provider_period == "daily" else f"{provider_period}m"
                            ),
                            "adjustment": "hfq",
                            "bars": bars,
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
                        return value
                    except Exception as exc:
                        errors.append(f"{provider.source}: {exc}")
                if cached is not None and self._usable_stale(cached, now):
                    value = dict(cached["value"])
                    value["status"] = {
                        **value["status"],
                        "delayed": True,
                        "stale": True,
                        "message": "; ".join(errors),
                    }
                    return value
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
    ) -> dict[str, Any]:
        """Warm quote and K-line caches without coupling live data to PIT snapshots."""

        normalized = sorted(set(normalize_symbol(item) for item in symbols if item.strip()))
        if not normalized:
            raise ValueError("at least one symbol is required")
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
        task_count = 1 + len(normalized) * len(requested_periods)
        max_workers = min(self.settings.market_prefetch_max_workers, task_count)
        with ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="market-prefetch",
        ) as pool:
            quote_future = pool.submit(self.quotes, normalized)
            kline_futures = {
                pool.submit(self.klines, symbol, period, limit=limit): (symbol, period)
                for symbol in normalized
                for period in requested_periods
            }
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
            "primary": self.primary.source,
            "fallback": self.fallback.source if self.fallback else None,
            "fallbacks": [provider.source for provider in self.fallbacks],
            "cache_seconds": self.settings.market_cache_seconds,
            "kline_cache_seconds": self.settings.market_kline_cache_seconds,
            "prefetch_max_workers": self.settings.market_prefetch_max_workers,
            "prefetch_max_symbols": MAX_PREFETCH_SYMBOLS,
            "stale_seconds": self.settings.market_stale_seconds,
            "adjustment": "hfq",
            "live_data_isolated_from_snapshots": True,
            "quotes": quote_status,
        }


@lru_cache(maxsize=1)
def get_market_data_service() -> MarketDataService:
    return MarketDataService()
