from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ashare_ai.adapters.symbols import normalize_symbol
from ashare_ai.core.config import Settings, get_settings
from ashare_ai.core.hashing import stable_hash

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
    mode: Literal["cli", "embedded"]
    searched_at: datetime
    elapsed_ms: int = Field(ge=0)
    entities: tuple[SearchEntity, ...]
    recalls: tuple[SearchRecall, ...]
    raw_sha256: str = Field(min_length=64, max_length=64)
    live_data_isolated_from_snapshots: bool = True


class FinancialSearchStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    upstream: str
    mode: Literal["cli", "embedded"]
    available: bool
    script_path: str | None = None
    message: str
    live_data_isolated_from_snapshots: bool = True


class FinancialSearchBusyError(RuntimeError):
    pass


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
            script_path=str(self.script_path) if self.script_path else None,
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

    def search(self, query: str) -> FinancialSearchResponse:
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
                result = self.provider.search(query)
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
                self._rate_events = {
                    key: value for key, value in self._rate_events.items() if value
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

    def status(self) -> FinancialSearchStatus:
        return self.provider.status()


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
    return FinancialSearchService()
