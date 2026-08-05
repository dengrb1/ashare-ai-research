from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ashare_ai.adapters.symbols import normalize_symbol
from ashare_ai.agents.model_settings import ModelConfigurationService, ModelSettingsError
from ashare_ai.agents.openai_compatible import OpenAICompatibleStructuredLLMClient
from ashare_ai.core.config import Settings, get_settings
from ashare_ai.core.hashing import stable_hash
from ashare_ai.core.system_settings import get_effective_settings

_PROVIDER_ALIASES = {
    "neodata": "neodata-financial-search",
    "neodata-financial-search": "neodata-financial-search",
    "neodate-financial-search": "neodata-financial-search",
}

_QUERY_CODES = {
    "贵州茅台": "sh600519",
    "茅台": "sh600519",
    "比亚迪": "sz002594",
    "宁德时代": "sz300750",
    "中国平安": "sh601318",
    "工商银行": "sh601398",
    "建设银行": "sh601939",
    "中石油": "sh601857",
    "中石化": "sh600028",
    "上证指数": "s_sh000001",
    "沪指": "s_sh000001",
    "深证成指": "s_sz399001",
    "创业板指": "s_sz399006",
    "沪深300": "s_sh000300",
}


class SearchEntity(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    name: str
    code: str


class SearchRecall(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    type: str
    desc: str
    content: str


class FinancialSearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str
    provider: str
    upstream: str
    mode: Literal["cli", "embedded", "direct", "ai"]
    searched_at: datetime
    elapsed_ms: int = Field(ge=0)
    entities: tuple[SearchEntity, ...]
    recalls: tuple[SearchRecall, ...]
    raw_sha256: str = Field(min_length=64, max_length=64)
    outcome: dict[str, Any] = Field(default_factory=dict)
    interpretation: str = ""
    sources: tuple[dict[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()
    live_data_isolated_from_snapshots: bool = True


class FinancialSearchStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    upstream: str
    mode: Literal["cli", "embedded", "direct", "ai"]
    available: bool
    configured: bool = False
    reachable: bool = False
    degraded: bool = False
    model: str | None = None
    script_path: str | None = None
    message: str
    live_data_isolated_from_snapshots: bool = True


class FinancialSearchBusyError(RuntimeError):
    pass


class FinancialQueryIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entity_name: str
    symbol: str
    asset_type: Literal["stock", "index"]
    data_kind: Literal["quote", "valuation", "kline", "financial"]
    period: Literal["day", "week", "month"] | None = None
    start_date: date | None = None
    end_date: date | None = None


class NeoDataFinancialSearchProvider:
    source = "neodata-financial-search"

    def __init__(
        self,
        *,
        script_path: Path | None,
        mode: Literal["auto", "cli", "embedded"],
        timeout_seconds: float,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.script_path = self._discover_script(script_path)
        if mode == "cli" and self.script_path is None:
            raise RuntimeError(
                "NeoData CLI mode requires NEODATA_FINANCIAL_SEARCH_PATH pointing to query.py"
            )
        self.mode: Literal["cli", "embedded"] = (
            "cli" if mode != "embedded" and self.script_path is not None else "embedded"
        )

    @staticmethod
    def _discover_script(configured: Path | None) -> Path | None:
        candidates: list[Path] = []
        if configured is not None:
            candidates.append(configured)
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        candidates.extend(
            [
                codex_home / "skills" / "neodata-financial-search" / "query.py",
                Path.home() / ".codex" / "skills" / "neodata-financial-search" / "query.py",
            ]
        )
        for candidate in candidates:
            path = candidate / "query.py" if candidate.is_dir() else candidate
            if path.is_file():
                return path.resolve()
        return None

    def status(self) -> FinancialSearchStatus:
        return FinancialSearchStatus(
            provider=self.source,
            upstream="sina-finance",
            mode=self.mode,
            available=True,
            # Keep the response field for v1 compatibility without exposing a
            # server filesystem path to authenticated remote clients.
            script_path=None,
            message=(
                "使用已发现的 NeoData query.py"
                if self.mode == "cli"
                else "未发现 NeoData CLI，使用新浪财经兼容查询模式"
            ),
        )

    def search(self, query: str) -> FinancialSearchResponse:
        normalized = query.strip()
        if not normalized:
            raise ValueError("financial search query cannot be empty")
        started = time.monotonic()
        searched_at = datetime.now(UTC)
        payload = (
            self._search_cli(normalized)
            if self.mode == "cli"
            else self._search_embedded(normalized)
        )
        api_data = payload.get("data", {}).get("apiData", {})
        entities = tuple(
            SearchEntity.model_validate(item) for item in api_data.get("entity", [])
        )
        recalls = tuple(
            SearchRecall.model_validate(item) for item in api_data.get("apiRecall", [])
        )
        if not recalls:
            raise RuntimeError("NeoData returned no financial search result")
        return FinancialSearchResponse(
            query=normalized,
            provider=self.source,
            upstream="sina-finance",
            mode=self.mode,
            searched_at=searched_at,
            elapsed_ms=max(0, round((time.monotonic() - started) * 1000)),
            entities=entities,
            recalls=recalls,
            raw_sha256=stable_hash(payload),
            outcome={"kind": "quote", "payload": payload},
            interpretation="确定性行情源返回的最新公开行情。",
            sources=(
                {
                    "source": "sina-finance",
                    "uri": "https://hq.sinajs.cn/",
                    "fetched_at": searched_at.isoformat(),
                },
            ),
        )

    def _search_cli(self, query: str) -> dict[str, Any]:
        if self.script_path is None:
            raise RuntimeError("NeoData query.py is unavailable")
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(self.script_path),
                    "--query",
                    query,
                    "--timeout",
                    str(max(1, int(self.timeout_seconds))),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds + 5,
                check=False,
                creationflags=flags,
                env=_minimal_subprocess_env(),
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("NeoData financial search timed out") from exc
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(f"NeoData financial search failed: {message[-500:]}")
        marker = "=== 查询结果 ==="
        raw = completed.stdout.split(marker, 1)[-1].strip()
        if not raw.startswith("{"):
            start = completed.stdout.find("{")
            raw = completed.stdout[start:].strip() if start >= 0 else raw
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError("NeoData CLI returned invalid JSON") from exc
        if not isinstance(payload, dict) or payload.get("suc") is not True:
            raise RuntimeError(f"NeoData search rejected the query: {payload}")
        return payload

    def _search_embedded(self, query: str) -> dict[str, Any]:
        code = _resolve_query_code(query)
        if code is None:
            return _neodata_payload(
                query,
                "SEARCH",
                "搜索结果",
                "未识别到证券代码。请输入六位 A 股代码，"
                "或配置 NeoData query.py 获取完整自然语言能力。",
            )
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; ashare-ai-research/0.1)",
            "Referer": "https://finance.sina.com.cn/",
        }
        try:
            response = httpx.get(
                f"https://hq.sinajs.cn/list={code}",
                headers=headers,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise TimeoutError("NeoData compatible search timed out") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"NeoData compatible upstream failed: {exc}") from exc
        text = response.content.decode("gbk", errors="replace").strip()
        match = re.search(r'="([^"]*)"', text)
        if match is None or not match.group(1):
            raise RuntimeError(f"NeoData compatible search found no data for {query}")
        content = _format_sina_content(code, match.group(1).split(","))
        return _neodata_payload(query, code, "行情数据", content)


class FinancialSearchService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        provider_name = _PROVIDER_ALIASES.get(
            self.settings.financial_search_provider.casefold()
        )
        if provider_name != "neodata-financial-search":
            raise RuntimeError(
                f"unsupported financial search provider: {self.settings.financial_search_provider}"
            )
        self.provider = NeoDataFinancialSearchProvider(
            script_path=self.settings.neodata_financial_search_path,
            mode=self.settings.neodata_financial_search_mode,
            timeout_seconds=self.settings.neodata_financial_search_timeout_seconds,
        )
        self._cache: dict[str, tuple[float, FinancialSearchResponse]] = {}
        self._query_locks: dict[str, threading.Lock] = {}
        self._rate_events: dict[str, deque[float]] = {}
        self._guard = threading.Lock()
        self._capacity = threading.BoundedSemaphore(
            self.settings.financial_search_max_concurrency
        )

    def search(self, query: str, session: Session | None = None) -> FinancialSearchResponse:
        key = query.strip().casefold()
        cached = self._cached(key)
        if cached is not None:
            return cached
        with self._guard:
            query_lock = self._query_locks.setdefault(key, threading.Lock())
        with query_lock:
            cached = self._cached(key)
            if cached is not None:
                return cached
            if not self._capacity.acquire(timeout=0.25):
                raise FinancialSearchBusyError("financial search is busy; retry shortly")
            try:
                result = self._search_uncached(query, session)
            finally:
                self._capacity.release()
            with self._guard:
                self._cache[key] = (time.monotonic(), result)
                if len(self._cache) > 512:
                    oldest = min(self._cache, key=lambda item: self._cache[item][0])
                    self._cache.pop(oldest, None)
                    self._query_locks.pop(oldest, None)
            return result

    def allow_user_request(self, user_id: str) -> bool:
        now = time.monotonic()
        cutoff = now - 60
        with self._guard:
            events = self._rate_events.setdefault(user_id, deque())
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.settings.financial_search_rate_limit_per_minute:
                return False
            events.append(now)
            if len(self._rate_events) > 10_000:
                # Purge users whose newest event already fell out of the window.
                # Every deque always holds its latest timestamp, so filtering on
                # non-emptiness alone never removes anything.
                self._rate_events = {
                    key: value
                    for key, value in self._rate_events.items()
                    if value and value[-1] > cutoff
                }
            return True

    def _cached(self, key: str) -> FinancialSearchResponse | None:
        with self._guard:
            cached = self._cache.get(key)
            if cached is None:
                return None
            if time.monotonic() - cached[0] > self.settings.financial_search_cache_seconds:
                self._cache.pop(key, None)
                return None
            return cached[1]

    def status(self, session: Session | None = None) -> FinancialSearchStatus:
        legacy = self.provider.status()
        if session is None:
            return legacy
        try:
            runtime = ModelConfigurationService(self.settings).resolve(
                session, require_enabled=False
            )
            health = ModelConfigurationService(self.settings).status(session)
        except ModelSettingsError as exc:
            return legacy.model_copy(
                update={
                    "available": False,
                    "configured": True,
                    "reachable": False,
                    "degraded": True,
                    "message": str(exc),
                }
            )
        if runtime is None:
            return legacy.model_copy(
                update={
                    "configured": False,
                    "reachable": False,
                    "degraded": False,
                    "message": "未配置 AI 意图模型；仅支持证券代码和少量内置名称",
                }
            )
        return FinancialSearchStatus(
            provider="ai-intent-deterministic-data",
            upstream="AKShare / EastMoney / Sina / Tencent",
            mode="ai",
            available=bool(runtime.enabled),
            configured=True,
            reachable=bool(health["reachable"]),
            degraded=bool(health["degraded"]),
            model=runtime.search_model,
            message=str(health["message"]),
        )

    def _search_uncached(
        self, query: str, session: Session | None
    ) -> FinancialSearchResponse:
        direct = _direct_intent(query)
        if direct is not None:
            if direct.data_kind == "quote" and (
                getattr(self.provider, "mode", "embedded") == "embedded"
                or any(name.casefold() in query.casefold() for name in _QUERY_CODES)
            ):
                return self.provider.search(query)
            return _deterministic_search(query, direct, self.provider, mode="direct")
        if session is None:
            return self.provider.search(query)
        runtime = ModelConfigurationService(self.settings).resolve(session)
        if runtime is None:
            return self.provider.search(query)
        model, effort = runtime.model_for("search")
        client = OpenAICompatibleStructuredLLMClient(
            base_url=runtime.base_url,
            api_key=runtime.api_key,
            model=model,
            reasoning_effort=effort,
            timeout_seconds=runtime.timeout_seconds,
            cache_policy=runtime.profile_for(model).cache_policy,
        )
        generation = asyncio.run(
            client.generate_structured(
                schema=FinancialQueryIntent,
                messages=(
                    {
                        "role": "system",
                        "content": (
                            "把中文 A 股金融数据查询解析成一个实体和一个数据类型。"
                            "symbol 必须是带交易所后缀的证券代码，例如 600690.SH、"
                            "000001.SZ、000300.SH。只解析意图，不回答投资问题。"
                        ),
                    },
                    {"role": "user", "content": query},
                ),
                idempotency_key=stable_hash(
                    {
                        "kind": "financial-query-intent",
                        "query": query,
                        "configuration": runtime.config_sha256,
                    }
                ),
            )
        )
        intent = FinancialQueryIntent.model_validate(generation.output)
        return _deterministic_search(query, intent, self.provider, mode="ai")


def _direct_intent(query: str) -> FinancialQueryIntent | None:
    code = _resolve_query_code(query)
    if code is None:
        return None
    if code.startswith("s_"):
        raw = code[2:]
        asset_type: Literal["stock", "index"] = "index"
    else:
        raw = code
        asset_type = (
            "index"
            if any(name in query for name in ("指数", "沪指", "沪深300"))
            else "stock"
        )
    exchange = "SH" if raw.startswith("sh") else "SZ"
    symbol = f"{raw[2:]}.{exchange}"
    lowered = query.casefold()
    if any(word in lowered for word in ("财报", "营收", "利润", "roe", "毛利", "负债", "eps")):
        kind: Literal["quote", "valuation", "kline", "financial"] = "financial"
    elif any(word in lowered for word in ("估值", "市盈", "市净", "pe", "pb", "市值")):
        kind = "valuation"
    elif any(word in lowered for word in ("k线", "走势", "历史", "月线", "周线")):
        kind = "kline"
    else:
        kind = "quote"
    period: Literal["day", "week", "month"] | None = None
    if kind == "kline":
        period = "month" if "月" in query else "week" if "周" in query else "day"
    entity = next((name for name, value in _QUERY_CODES.items() if value == code), symbol)
    return FinancialQueryIntent(
        entity_name=entity,
        symbol=symbol,
        asset_type=asset_type,
        data_kind=kind,
        period=period,
        start_date=None,
        end_date=None,
    )


def _deterministic_search(
    query: str,
    intent: FinancialQueryIntent,
    legacy: NeoDataFinancialSearchProvider,
    *,
    mode: Literal["direct", "ai"],
) -> FinancialSearchResponse:
    started = time.monotonic()
    searched_at = datetime.now(UTC)
    symbol = _validated_intent_symbol(intent.symbol)
    if intent.data_kind == "quote":
        payload = legacy._search_embedded(symbol)
        api_data = payload.get("data", {}).get("apiData", {})
        recalls = tuple(
            SearchRecall.model_validate(item) for item in api_data.get("apiRecall", [])
        )
        outcome = {
            "kind": "quote",
            "symbol": symbol,
            "data": [item.model_dump(mode="json") for item in recalls],
        }
        return FinancialSearchResponse(
            query=query,
            provider="ai-intent-deterministic-data",
            upstream="sina-finance",
            mode=mode,
            searched_at=searched_at,
            elapsed_ms=max(0, round((time.monotonic() - started) * 1000)),
            entities=(SearchEntity(name=intent.entity_name, code=symbol),),
            recalls=recalls,
            raw_sha256=stable_hash(payload),
            outcome=outcome,
            interpretation=(
                "AI 仅用于识别查询意图；行情事实由新浪公开行情接口确定性获取。"
            ),
            sources=(
                {
                    "source": "sina-finance",
                    "uri": "https://hq.sinajs.cn/",
                    "fetched_at": searched_at.isoformat(),
                },
            ),
        )
    if intent.data_kind == "kline":
        from ashare_ai.market.service import get_market_data_service

        payload = get_market_data_service().klines(
            symbol,
            intent.period or "day",
            limit=120,
            start=(
                datetime.combine(intent.start_date, datetime.min.time(), tzinfo=UTC)
                if intent.start_date
                else None
            ),
            end=(
                datetime.combine(intent.end_date, datetime.max.time(), tzinfo=UTC)
                if intent.end_date
                else None
            ),
            adjustment="raw",
        )
        serializable = _json_value(payload)
        bars = serializable.get("bars", []) if isinstance(serializable, dict) else []
        compact = bars[-30:] if isinstance(bars, list) else bars
        source = payload.get("status", {}).get("source", "market-adapter")
        return _built_response(
            query=query,
            intent=intent,
            symbol=symbol,
            mode=mode,
            searched_at=searched_at,
            started=started,
            upstream=str(source),
            outcome={"kind": "kline", "symbol": symbol, "bars": serializable.get("bars", [])},
            content=json.dumps(compact, ensure_ascii=False, indent=2),
            source_uri="https://gu.qq.com/",
            interpretation="K 线由确定性行情适配器获取并按查询周期展示。",
        )
    if intent.asset_type == "index":
        raise ValueError("指数查询当前支持行情与 K 线，不支持估值或财务指标")
    if intent.data_kind == "valuation":
        facts, upstream, source_uri, warnings = _valuation_facts(symbol)
        return _built_response(
            query=query,
            intent=intent,
            symbol=symbol,
            mode=mode,
            searched_at=searched_at,
            started=started,
            upstream=upstream,
            outcome={"kind": "valuation", "symbol": symbol, "facts": facts},
            content=json.dumps(facts, ensure_ascii=False, indent=2),
            source_uri=source_uri,
            interpretation=(
                "估值指标来自公开行情接口，模型未生成或改写数值。"
            ),
            warnings=warnings,
        )
    facts, report_date, notice_date, warnings = _akshare_financials(symbol, searched_at)
    return _built_response(
        query=query,
        intent=intent,
        symbol=symbol,
        mode=mode,
        searched_at=searched_at,
        started=started,
        upstream="eastmoney",
        outcome={
            "kind": "financial",
            "symbol": symbol,
            "report_date": report_date,
            "notice_date": notice_date,
            "facts": facts,
        },
        content=json.dumps(facts, ensure_ascii=False, indent=2),
        source_uri="https://data.eastmoney.com/bbsj/",
        interpretation="展示最近一期已披露核心财务指标；未来披露记录已拒绝。",
        report_date=report_date,
        notice_date=notice_date,
        warnings=warnings,
    )


def _built_response(
    *,
    query: str,
    intent: FinancialQueryIntent,
    symbol: str,
    mode: Literal["direct", "ai"],
    searched_at: datetime,
    started: float,
    upstream: str,
    outcome: dict[str, Any],
    content: str,
    source_uri: str,
    interpretation: str,
    report_date: str | None = None,
    notice_date: str | None = None,
    warnings: tuple[str, ...] = (),
) -> FinancialSearchResponse:
    source = {
        "source": upstream,
        "uri": source_uri,
        "fetched_at": searched_at.isoformat(),
        "report_date": report_date,
        "notice_date": notice_date,
    }
    raw = {"intent": intent.model_dump(mode="json"), "outcome": outcome, "source": source}
    return FinancialSearchResponse(
        query=query,
        provider="ai-intent-deterministic-data",
        upstream=upstream,
        mode=mode,
        searched_at=searched_at,
        elapsed_ms=max(0, round((time.monotonic() - started) * 1000)),
        entities=(SearchEntity(name=intent.entity_name, code=symbol),),
        recalls=(SearchRecall(type=intent.data_kind, desc="金融数据", content=content),),
        raw_sha256=stable_hash(raw),
        outcome=outcome,
        interpretation=interpretation,
        sources=(source,),
        warnings=warnings,
    )


def _validated_intent_symbol(value: str) -> str:
    match = re.fullmatch(r"(\d{6})[.\-]?(SH|SZ|BJ)", value.strip(), re.IGNORECASE)
    if match is None:
        raise ValueError("AI 返回了无效证券代码")
    return f"{match.group(1)}.{match.group(2).upper()}"


def _akshare_valuation(symbol: str) -> dict[str, Any]:
    import akshare as ak

    frame = ak.stock_zh_a_spot_em()
    number = symbol.split(".", 1)[0]
    rows = frame.loc[frame["代码"].astype(str).str.zfill(6) == number]
    if rows.empty:
        raise RuntimeError(f"东方财富未返回 {symbol} 的估值数据")
    row = rows.iloc[0].to_dict()
    mapping = {
        "name": "名称",
        "price": "最新价",
        "pe_dynamic": "市盈率-动态",
        "pb": "市净率",
        "turnover_rate": "换手率",
        "total_market_cap": "总市值",
        "float_market_cap": "流通市值",
    }
    return {key: _json_value(row.get(column)) for key, column in mapping.items()}


def _valuation_facts(
    symbol: str,
) -> tuple[dict[str, Any], str, str, tuple[str, ...]]:
    """Read valuation facts with a free, independent fallback.

    AKShare's all-market EastMoney endpoint occasionally closes connections while
    a single-symbol quote remains available elsewhere.  A search should degrade
    across providers instead of leaking that transport failure as an HTTP 500.
    """

    try:
        return (
            _akshare_valuation(symbol),
            "eastmoney",
            "https://quote.eastmoney.com/",
            (),
        )
    except Exception:
        try:
            facts = _tencent_valuation(symbol)
        except Exception as tencent_exc:
            raise RuntimeError(
                "免费估值数据源暂时不可用，请稍后重试"
            ) from tencent_exc
        return (
            facts,
            "tencent-finance",
            "https://gu.qq.com/",
            ("东方财富实时估值接口暂不可用，已自动切换腾讯公开行情接口。",),
        )


def _tencent_valuation(symbol: str) -> dict[str, Any]:
    number, exchange = symbol.split(".", 1)
    code = f"{exchange.casefold()}{number}"
    response = httpx.get(
        f"https://qt.gtimg.cn/q={code}",
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; ashare-ai-research/0.1)",
            "Referer": "https://finance.qq.com/",
        },
        timeout=10.0,
    )
    response.raise_for_status()
    text = response.content.decode("gbk", errors="replace")
    match = re.search(r'="([^"]*)"', text)
    if match is None:
        raise RuntimeError(f"腾讯未返回 {symbol} 的估值数据")
    fields = match.group(1).split("~")
    if len(fields) <= 46 or fields[2] != number:
        raise RuntimeError(f"腾讯未返回 {symbol} 的完整估值数据")
    return {
        "name": fields[1],
        "price": _optional_number(fields[3]),
        "pe_dynamic": _optional_number(fields[39]),
        "pb": _optional_number(fields[46]),
        "turnover_rate": _optional_number(fields[38]),
        # Tencent publishes both market-cap fields in CNY 100 million.
        "total_market_cap": _scaled_number(fields[45], 100_000_000),
        "float_market_cap": _scaled_number(fields[44], 100_000_000),
    }


def _optional_number(value: Any) -> float | None:
    text = str(value).strip()
    if not text or text in {"-", "--"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _scaled_number(value: Any, scale: int) -> int | None:
    number = _optional_number(value)
    return round(number * scale) if number is not None else None


def _akshare_financials(
    symbol: str, decision_at: datetime
) -> tuple[dict[str, Any], str | None, str | None, tuple[str, ...]]:
    import akshare as ak

    number = symbol.split(".", 1)[0]
    frame = ak.stock_financial_analysis_indicator_em(
        symbol=number, start_year=str(decision_at.year - 3)
    )
    if frame.empty:
        raise RuntimeError(f"东方财富未返回 {symbol} 的财务指标")
    records = [_json_value(item) for item in frame.to_dict(orient="records")]
    visible = []
    for row in records:
        notice = _first_value(row, "NOTICE_DATE", "公告日期", "最新公告日期")
        parsed = _parse_optional_date(notice)
        if parsed is None or parsed <= decision_at.date():
            visible.append(row)
    if not visible:
        raise RuntimeError("财务指标仅包含查询时点之后披露的数据，已拒绝返回")
    visible.sort(
        key=lambda item: _date_text(
            _first_value(item, "REPORT_DATE", "报告期", "日期")
        )
        or "",
        reverse=True,
    )
    row = visible[0]
    report_date = _date_text(_first_value(row, "REPORT_DATE", "报告期", "日期"))
    notice_date = _date_text(_first_value(row, "NOTICE_DATE", "公告日期", "最新公告日期"))
    mapping = {
        "revenue": ("TOTALOPERATEREVE", "营业总收入"),
        "parent_net_profit": ("PARENTNETPROFIT", "归属母公司净利润"),
        "revenue_yoy": ("TOTALOPERATEREVETZ", "营业总收入同比增长"),
        "profit_yoy": ("PARENTNETPROFITTZ", "归属母公司净利润同比增长"),
        "eps": ("EPSJB", "基本每股收益"),
        "roe": ("ROEJQ", "净资产收益率(加权)"),
        "gross_margin": ("XSMLL", "销售毛利率"),
        "debt_ratio": ("ZCFZL", "资产负债率"),
    }
    facts = {key: _first_value(row, *columns) for key, columns in mapping.items()}
    warnings = () if notice_date else ("上游记录未提供公告日期，已保留报告期与抓取时间。",)
    return facts, report_date, notice_date, warnings


def _first_value(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, "", "--"):
            return value
    return None


def _parse_optional_date(value: Any) -> date | None:
    text = _date_text(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _date_text(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    text = str(value).strip()
    return text[:10] if text and text.casefold() != "nat" else None


def _json_value(value: Any) -> Any:
    if type(value).__name__ in {"NAType", "NaTType"}:
        return None
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return _json_value(value.item())
        except (TypeError, ValueError):
            pass
    try:
        missing = value != value
        if isinstance(missing, bool) and missing:
            return None
    except (TypeError, ValueError):
        pass
    return value


def _resolve_query_code(query: str) -> str | None:
    for name, code in sorted(_QUERY_CODES.items(), key=lambda item: len(item[0]), reverse=True):
        if name.casefold() in query.casefold():
            return code
    match = re.search(r"(?<!\d)(\d{6})(?:[.\-]?(SH|SZ|BJ))?(?!\d)", query, re.IGNORECASE)
    if match is None:
        return None
    symbol = normalize_symbol(match.group(1), match.group(2))
    number, exchange = symbol.split(".", 1)
    if exchange == "BJ":
        return None
    return f"{exchange.casefold()}{number}"


def _format_sina_content(code: str, parts: list[str]) -> str:
    if code.startswith("s_") and len(parts) >= 6:
        return (
            f"名称: {parts[0]}\n最新: {parts[1]}\n涨跌额: {parts[2]}\n"
            f"涨跌幅: {parts[3]}%\n成交量: {parts[4]}\n成交额: {parts[5]}"
        )
    if len(parts) >= 10:
        date_value = parts[30] if len(parts) > 30 else ""
        time_value = parts[31] if len(parts) > 31 else ""
        return (
            f"名称: {parts[0]}\n开盘: {parts[1]}\n昨收: {parts[2]}\n最新: {parts[3]}\n"
            f"最高: {parts[4]}\n最低: {parts[5]}\n成交量: {parts[8]}\n"
            f"成交额: {parts[9]}\n时间: {date_value} {time_value}".rstrip()
        )
    return "原始行情: " + ",".join(parts[:20])


def _neodata_payload(query: str, code: str, desc: str, content: str) -> dict[str, Any]:
    return {
        "code": "200",
        "msg": "操作成功",
        "suc": True,
        "data": {
            "apiData": {
                "entity": [{"name": query, "code": code}],
                "apiRecall": [{"type": "basic_info", "desc": desc, "content": content}],
            },
            "docData": {"docRecall": []},
            "se_params": {},
            "extra_params": {},
        },
    }


def _minimal_subprocess_env() -> dict[str, str]:
    allowed = (
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
        "LANG",
        "LC_ALL",
        "SSL_CERT_FILE",
        "REQUESTS_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
    )
    result = {key: os.environ[key] for key in allowed if os.environ.get(key)}
    result.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    return result


@lru_cache(maxsize=1)
def get_financial_search_service() -> FinancialSearchService:
    return FinancialSearchService(settings=get_effective_settings())
