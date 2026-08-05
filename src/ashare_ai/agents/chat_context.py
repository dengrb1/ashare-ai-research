from __future__ import annotations

import copy
import json
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import and_, or_, select

from ashare_ai.core.config import Settings, get_settings
from ashare_ai.core.hashing import stable_hash
from ashare_ai.market.service import get_market_data_service
from ashare_ai.portfolio.user_assets import UserAssetService
from ashare_ai.reports.chinese_summary import symbol_summary
from ashare_ai.search.news import NewsSearchService
from ashare_ai.storage.database import SessionLocal
from ashare_ai.storage.models import (
    CandidateRow,
    JobRun,
    ReportRow,
    ScoreRow,
    SecurityMaster,
    SnapshotManifestRow,
    UserAssetState,
)

_MENTION = re.compile(
    r"@([0-9]{6}(?:\.(?:SH|SZ|BJ))?|[\u4e00-\u9fffA-Za-z*][\u4e00-\u9fffA-Za-z0-9*._-]{1,63})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MentionResolution:
    refs: list[dict[str, str]]
    statuses: list[dict[str, str]]


@dataclass(frozen=True)
class ChatContextResult:
    context: dict[str, Any]
    sources: list[dict[str, Any]]
    decision_at: datetime
    data_status: dict[str, Any]
    context_cache_hit: bool
    market_cache_hit: bool
    news_cache_hit: bool
    singleflight_wait_ms: int


def symbol_from_code(value: str) -> str | None:
    raw = value.strip().upper()
    if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", raw):
        return raw
    if not re.fullmatch(r"\d{6}", raw):
        return None
    suffix = "SH" if raw.startswith("6") else "BJ" if raw.startswith(("4", "8")) else "SZ"
    return f"{raw}.{suffix}"


def resolve_security_mentions(
    content: str,
    requested_refs: list[dict[str, str]],
    *,
    decision_at: datetime,
    session_factory: Callable[[], Any] = SessionLocal,
) -> MentionResolution:
    """Resolve textual stock mentions against point-in-time SecurityMaster records.

    Client-provided mention refs are hints only.  A name never binds to a symbol
    unless the master has exactly one active symbol with that exact name at the
    requested time.  This deliberately rejects ambiguous names instead of using
    a user's watchlist or trusting a browser-supplied code/name pair.
    """

    as_of = decision_at.astimezone(UTC)
    raws = [match.group(1).strip() for match in _MENTION.finditer(content)]
    for item in requested_refs:
        symbol = str(item.get("symbol") or "").strip().upper()
        name = str(item.get("name") or "").strip()
        if symbol and f"@{symbol}".casefold() in content.casefold():
            raws.append(symbol)
        elif name and f"@{name}" in content:
            raws.append(name)
    unique_raws = list(dict.fromkeys(raw for raw in raws if raw))[:5]
    refs: list[dict[str, str]] = []
    statuses: list[dict[str, str]] = []
    with session_factory() as session:
        for raw in unique_raws:
            standard_code = bool(re.fullmatch(r"\d{6}", raw))
            lookup_symbol: str | None = (
                raw.upper() if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", raw, re.I) else None
            )
            rows = _active_master_rows(
                session,
                decision_at=as_of,
                symbol=lookup_symbol,
                code=raw if standard_code else None,
                name=None if lookup_symbol else raw,
            )
            by_symbol = {row.symbol: row for row in rows}
            if not by_symbol:
                statuses.append(
                    {"mention": raw, "state": "MISSING", "reason_code": "SECURITY_MASTER_NOT_FOUND"}
                )
                continue
            if len(by_symbol) != 1:
                statuses.append(
                    {
                        "mention": raw,
                        "state": "AMBIGUOUS",
                        "reason_code": (
                            "SECURITY_CODE_AMBIGUOUS"
                            if standard_code
                            else "SECURITY_NAME_AMBIGUOUS"
                        ),
                    }
                )
                continue
            row = next(iter(by_symbol.values()))
            if row.symbol not in {item["symbol"] for item in refs}:
                refs.append({"symbol": row.symbol, "name": row.short_name})
            statuses.append(
                {
                    "mention": raw,
                    "state": "RESOLVED",
                    "reason_code": "OK",
                    "symbol": row.symbol,
                }
            )
    return MentionResolution(refs=refs, statuses=statuses)


def _active_master_rows(
    session: Any,
    *,
    decision_at: datetime,
    symbol: str | None,
    code: str | None,
    name: str | None,
) -> list[SecurityMaster]:
    statement = select(SecurityMaster).where(
        SecurityMaster.available_at <= decision_at,
        SecurityMaster.effective_from <= decision_at.date(),
        or_(
            SecurityMaster.effective_to.is_(None),
            SecurityMaster.effective_to >= decision_at.date(),
        ),
    )
    if symbol is not None:
        statement = statement.where(SecurityMaster.symbol == symbol)
    elif code is not None:
        statement = statement.where(SecurityMaster.symbol.like(f"{code}.%"))
    elif name is not None:
        statement = statement.where(SecurityMaster.short_name == name)
    else:
        return []
    rows = list(
        session.scalars(
            statement.order_by(SecurityMaster.available_at.desc(), SecurityMaster.fetched_at.desc())
        ).all()
    )
    latest: dict[str, SecurityMaster] = {}
    for row in rows:
        latest.setdefault(row.symbol, row)
    return list(latest.values())


class ChatContextService:
    """Build a small, point-in-time chat context with bounded live-data work."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        market: Any | None = None,
        news: NewsSearchService | None = None,
        session_factory: Callable[[], Any] = SessionLocal,
    ) -> None:
        self.settings = settings or get_settings()
        self.market = market or get_market_data_service()
        self.news = news or NewsSearchService(settings=self.settings)
        self.session_factory = session_factory
        policy = _chat_policy(self.settings)
        self.cache_seconds = _positive_int(policy.get("context_cache_seconds"), 120, 15, 3600)
        self.max_workers = _positive_int(
            policy.get("max_daily_kline_concurrency"),
            self.settings.market_prefetch_max_workers,
            1,
            16,
        )
        self.news_window_days = _positive_int(policy.get("news_window_days"), 30, 1, 365)
        self.max_news_results = _positive_int(policy.get("max_news_results"), 5, 1, 5)
        self._cache: OrderedDict[str, tuple[float, ChatContextResult]] = OrderedDict()
        self._cache_lock = threading.Lock()
        self._inflight: dict[str, threading.Event] = {}
        self._max_entries = 256

    def build(
        self,
        *,
        user_id: str,
        refs: list[dict[str, str]],
        requested_decision_at: datetime | None,
        web_search: bool,
        model_configuration_sha256: str | None,
    ) -> ChatContextResult:
        historical = requested_decision_at is not None
        requested = requested_decision_at.astimezone(UTC) if requested_decision_at else None
        symbols = [item["symbol"] for item in refs]
        asset_version = self._asset_version(user_id)
        research_version = self._published_research_version(user_id)
        cache_key = stable_hash(
            {
                "user_id": user_id,
                "asset_updated_at": asset_version,
                "published_research_at": research_version,
                "symbols": refs,
                "decision_at": (
                    requested.isoformat() if requested else _live_bucket(self.cache_seconds)
                ),
                "historical": historical,
                "web_search": web_search,
                "model_configuration": model_configuration_sha256,
            }
        )
        started = time.monotonic()
        with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached is not None and cached[0] > time.monotonic():
                self._cache.move_to_end(cache_key)
                return _with_context_cache_hit(cached[1], wait_ms=0)
            gate = self._inflight.get(cache_key)
            if gate is None:
                gate = threading.Event()
                self._inflight[cache_key] = gate
                owner = True
            else:
                owner = False
        if not owner:
            timeout = (
                self.settings.market_timeout_seconds + self.settings.searxng_timeout_seconds + 4
            )
            gate.wait(timeout=timeout)
            waited = round((time.monotonic() - started) * 1000)
            with self._cache_lock:
                cached = self._cache.get(cache_key)
                if cached is not None and cached[0] > time.monotonic():
                    self._cache.move_to_end(cache_key)
                    return _with_context_cache_hit(cached[1], wait_ms=waited)
            # A failed owner must never return a stale or partial context from another user.
            return self._minimal_unavailable_context(
                symbols=symbols,
                requested_decision_at=requested,
                wait_ms=waited,
            )
        try:
            result = self._build_uncached(
                user_id=user_id,
                refs=refs,
                requested_decision_at=requested,
                historical=historical,
                web_search=web_search,
            )
            with self._cache_lock:
                self._cache[cache_key] = (time.monotonic() + self.cache_seconds, result)
                self._cache.move_to_end(cache_key)
                while len(self._cache) > self._max_entries:
                    self._cache.popitem(last=False)
            return result
        finally:
            with self._cache_lock:
                completion = self._inflight.pop(cache_key, None)
                if completion is not None:
                    completion.set()

    def _asset_version(self, user_id: str) -> str | None:
        with self.session_factory() as session:
            row = session.get(UserAssetState, user_id)
            return row.updated_at.astimezone(UTC).isoformat() if row and row.updated_at else None

    def _published_research_version(self, user_id: str) -> str | None:
        with self.session_factory() as session:
            row = session.scalar(
                select(ReportRow.created_at)
                .join(JobRun, JobRun.run_id == ReportRow.run_id)
                .where(
                    JobRun.user_id == user_id,
                    JobRun.status.in_(("SUCCEEDED", "FUSED")),
                    JobRun.completed_at.is_not(None),
                    ReportRow.report_type == "DAILY_RESEARCH",
                )
                .order_by(ReportRow.created_at.desc())
                .limit(1)
            )
            return row.astimezone(UTC).isoformat() if row else None

    def _build_uncached(
        self,
        *,
        user_id: str,
        refs: list[dict[str, str]],
        requested_decision_at: datetime | None,
        historical: bool,
        web_search: bool,
    ) -> ChatContextResult:
        provisional = requested_decision_at or datetime.now(UTC)
        symbols = [item["symbol"] for item in refs]
        names = {item["symbol"]: item["name"] for item in refs}
        scores, score_status, positions, position_status = self._database_context(
            user_id=user_id, symbols=symbols, decision_at=provisional, historical=historical
        )
        quotes: dict[str, Any] = {}
        bars: dict[str, list[dict[str, Any]]] = {}
        statuses: dict[str, Any] = {
            "positions": position_status,
            "scores": score_status,
            "quotes": {},
            "daily_bars": {},
            "news": {},
        }
        latest_research, research_status, research_sources = self._latest_published_research(
            user_id=user_id,
            symbols=symbols,
            decision_at=provisional,
        )
        statuses["latest_published_research"] = research_status
        news_items: dict[str, list[dict[str, Any]]] = {}
        sources: list[dict[str, Any]] = research_sources
        market_cache_hit = False
        news_cache_hit = False
        future_count = (1 if symbols and not historical else 0) + len(symbols)
        if web_search and not historical:
            future_count += len(symbols)
        workers = max(1, min(self.max_workers, future_count or 1))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            quote_future = (
                pool.submit(self.market.quotes, symbols) if symbols and not historical else None
            )
            kline_futures = {
                (
                    pool.submit(
                        self._historical_daily_bars,
                        user_id=user_id,
                        symbol=symbol,
                        decision_at=provisional,
                    )
                    if historical
                    else pool.submit(
                        self.market.klines,
                        symbol,
                        "day",
                        limit=30,
                        end=provisional,
                        adjustment="raw",
                    )
                ): symbol
                for symbol in symbols
            }
            news_futures = {
                pool.submit(
                    self.news.search_for_security,
                    symbol=symbol,
                    name=names.get(symbol, symbol),
                    max_results=self.max_news_results,
                    window_days=self.news_window_days,
                ): symbol
                for symbol in symbols
                if web_search and not historical
            }
            if quote_future is not None:
                try:
                    quote_rows = quote_future.result()
                except Exception:
                    quote_rows = []
                    quote_upstream_unavailable = True
                else:
                    quote_upstream_unavailable = False
                quote_by_symbol = {
                    str(item.get("symbol")): item
                    for item in quote_rows
                    if isinstance(item, dict) and isinstance(item.get("symbol"), str)
                }
                for symbol in symbols:
                    quote = quote_by_symbol.get(symbol)
                    if quote is None:
                        statuses["quotes"][symbol] = {
                            "state": ("UNAVAILABLE" if quote_upstream_unavailable else "MISSING"),
                            "reason_code": (
                                "QUOTE_UPSTREAM_UNAVAILABLE"
                                if quote_upstream_unavailable
                                else "QUOTE_MISSING"
                            ),
                            "source": "market",
                        }
                        continue
                    raw_status = quote.get("status")
                    status: dict[str, Any] = (
                        dict(raw_status) if isinstance(raw_status, dict) else {}
                    )
                    if status.get("stale") or status.get("delayed"):
                        state, reason = "STALE", "QUOTE_STALE"
                    elif quote.get("price") is None:
                        state, reason = "MISSING", "QUOTE_PRICE_MISSING"
                    else:
                        state, reason = "AVAILABLE", "OK"
                    statuses["quotes"][symbol] = {
                        "state": state,
                        "reason_code": reason,
                        "source": status.get("source", "market"),
                        "available_at": status.get("collected_at"),
                    }
                    quotes[symbol] = quote
                    market_cache_hit = market_cache_hit or bool(status.get("cache_hit"))
                    sources.append(
                        {
                            "source": "market",
                            "symbol": symbol,
                            "uri": f"/api/v1/market/quotes?symbols={symbol}",
                            "available_at": status.get("collected_at"),
                            "status": state,
                        }
                    )
            else:
                for symbol in symbols:
                    statuses["quotes"][symbol] = {
                        "state": "UNAVAILABLE",
                        "reason_code": "HISTORICAL_LIVE_QUOTE_EXCLUDED",
                        "source": "market",
                    }
            for future in as_completed(kline_futures):
                symbol = kline_futures[future]
                try:
                    payload = future.result()
                    raw_bars = payload.get("bars", []) if isinstance(payload, dict) else []
                    status = payload.get("status", {}) if isinstance(payload, dict) else {}
                    bars[symbol] = list(raw_bars)[-30:] if isinstance(raw_bars, list) else []
                    source = (
                        status.get("source", "market") if isinstance(status, dict) else "market"
                    )
                    available_at = (
                        status.get("available_at", status.get("collected_at"))
                        if isinstance(status, dict)
                        else None
                    )
                    state = (
                        str(status.get("state"))
                        if historical and isinstance(status, dict) and status.get("state")
                        else "AVAILABLE"
                        if bars[symbol]
                        else "MISSING"
                    )
                    reason = (
                        str(status.get("reason_code"))
                        if historical and isinstance(status, dict) and status.get("reason_code")
                        else "OK"
                        if bars[symbol]
                        else "KLINE_EMPTY"
                    )
                    statuses["daily_bars"][symbol] = {
                        "state": state,
                        "reason_code": reason,
                        "source": source,
                        "available_at": available_at,
                    }
                    if isinstance(status, dict):
                        market_cache_hit = market_cache_hit or bool(status.get("cache_hit"))
                    sources.append(
                        {
                            "source": source,
                            "symbol": symbol,
                            "uri": (
                                f"/api/v1/market/klines/{symbol}?period=day"
                                if not historical
                                else None
                            ),
                            "available_at": statuses["daily_bars"][symbol]["available_at"],
                            "status": statuses["daily_bars"][symbol]["state"],
                        }
                    )
                except Exception:
                    bars[symbol] = []
                    statuses["daily_bars"][symbol] = {
                        "state": "UNAVAILABLE",
                        "reason_code": (
                            "PIT_KLINE_MANIFEST_UNAVAILABLE"
                            if historical
                            else "KLINE_UPSTREAM_UNAVAILABLE"
                        ),
                        "source": "committed_manifest" if historical else "market",
                    }
            for future in as_completed(news_futures):
                symbol = news_futures[future]
                try:
                    result = future.result()
                except Exception:
                    news_items[symbol] = []
                    statuses["news"][symbol] = {
                        "state": "UNAVAILABLE",
                        "reason_code": "NEWS_UPSTREAM_UNAVAILABLE",
                        "source": "searxng",
                    }
                    continue
                news_items[symbol] = result.items
                statuses["news"][symbol] = result.status
                news_cache_hit = news_cache_hit or result.cache_hit
                for item in result.items:
                    sources.append(
                        {
                            "source": "searxng",
                            "symbol": symbol,
                            "title": item["title"],
                            "uri": item["url"],
                            "engine": item["engine"],
                            "available_at": item.get("published_at"),
                        }
                    )
        if historical or not web_search:
            reason = "HISTORICAL_NEWS_EXCLUDED" if historical else "NEWS_DISABLED"
            for symbol in symbols:
                statuses["news"].setdefault(
                    symbol,
                    {"state": "UNAVAILABLE", "reason_code": reason, "source": "searxng"},
                )
                news_items.setdefault(symbol, [])
        # A live request is frozen only after its parallel data retrieval finishes.
        decision_at = provisional if historical else datetime.now(UTC)
        context = {
            "symbols": symbols,
            "positions": positions,
            "quotes": quotes,
            "daily_bars": bars,
            "latest_formal_scores": scores,
            "latest_published_research": latest_research,
            "news": news_items,
            "decision_at": decision_at.isoformat(),
            "historical": historical,
            "data_status": statuses,
        }
        return ChatContextResult(
            context=context,
            sources=sources,
            decision_at=decision_at,
            data_status=statuses,
            context_cache_hit=False,
            market_cache_hit=market_cache_hit,
            news_cache_hit=news_cache_hit,
            singleflight_wait_ms=0,
        )

    def _database_context(
        self,
        *,
        user_id: str,
        symbols: list[str],
        decision_at: datetime,
        historical: bool,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
        scores: dict[str, Any] = {}
        score_status: dict[str, Any] = {}
        with self.session_factory() as session:
            positions: dict[str, Any]
            position_status: dict[str, Any]
            if historical:
                positions = {}
                position_status = {
                    "state": "UNAVAILABLE",
                    "reason_code": "HISTORICAL_CURRENT_POSITIONS_EXCLUDED",
                }
            else:
                assets = UserAssetService(session).get(user_id)
                positions = {
                    str(item.get("symbol")): dict(item)
                    for item in assets.get("positions", [])
                    if item.get("symbol") in symbols
                }
                position_status = {
                    "state": "AVAILABLE",
                    "reason_code": "OK",
                    "asset_updated_at": (
                        assets["updated_at"].astimezone(UTC).isoformat()
                        if assets.get("updated_at")
                        else None
                    ),
                }
            for symbol in symbols:
                row = session.scalar(
                    select(ScoreRow)
                    .join(JobRun, JobRun.run_id == ScoreRow.run_id)
                    .where(
                        ScoreRow.symbol == symbol,
                        JobRun.user_id == user_id,
                        JobRun.status.in_(("SUCCEEDED", "FUSED")),
                        JobRun.decision_at <= decision_at,
                        ScoreRow.decision_at <= decision_at,
                        JobRun.completed_at.is_not(None),
                        JobRun.completed_at <= decision_at,
                    )
                    .order_by(JobRun.trading_date.desc(), JobRun.completed_at.desc())
                    .limit(1)
                )
                if row is None:
                    score_status[symbol] = {
                        "state": "MISSING",
                        "reason_code": "PIT_SCORE_NOT_FOUND",
                    }
                    continue
                scores[symbol] = {
                    "trading_date": row.trading_date.isoformat(),
                    "decision_at": row.decision_at.isoformat(),
                    "total_score": row.total_score,
                    "fundamental_score": row.fundamental_score,
                    "technical_score": row.technical_score,
                    "sentiment_score": row.sentiment_score,
                    "event_risk_multiplier": row.event_risk_multiplier,
                    "formula_version": row.formula_version,
                }
                score_status[symbol] = {"state": "AVAILABLE", "reason_code": "OK"}
        return scores, score_status, positions, position_status

    def _latest_published_research(
        self,
        *,
        user_id: str,
        symbols: list[str],
        decision_at: datetime,
    ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        """Read bounded published-report metadata without opening report HTML."""

        with self.session_factory() as session:
            if symbols:
                rows: dict[str, Any] = {}
                statuses: dict[str, Any] = {}
                sources: list[dict[str, Any]] = []
                for symbol in symbols:
                    item = session.execute(
                        select(ScoreRow, CandidateRow, JobRun, ReportRow)
                        .join(JobRun, JobRun.run_id == ScoreRow.run_id)
                        .join(ReportRow, ReportRow.run_id == JobRun.run_id)
                        .outerjoin(
                            CandidateRow,
                            and_(
                                CandidateRow.run_id == ScoreRow.run_id,
                                CandidateRow.symbol == ScoreRow.symbol,
                            ),
                        )
                        .where(
                            ScoreRow.symbol == symbol,
                            JobRun.user_id == user_id,
                            JobRun.status.in_(("SUCCEEDED", "FUSED")),
                            JobRun.decision_at <= decision_at,
                            ScoreRow.decision_at <= decision_at,
                            JobRun.completed_at.is_not(None),
                            JobRun.completed_at <= decision_at,
                            ReportRow.created_at <= decision_at,
                            ReportRow.report_type == "DAILY_RESEARCH",
                        )
                        .order_by(
                            JobRun.trading_date.desc(),
                            JobRun.completed_at.desc(),
                            ReportRow.created_at.desc(),
                        )
                        .limit(1)
                    ).first()
                    if item is None:
                        statuses[symbol] = {
                            "state": "MISSING",
                            "reason_code": "PUBLISHED_RESEARCH_NOT_FOUND",
                        }
                        continue
                    score, candidate, run, report = item
                    entry = self._published_symbol_research_entry(score, candidate, run, report)
                    rows[symbol] = entry
                    statuses[symbol] = {
                        "state": "AVAILABLE",
                        "reason_code": "OK",
                        "report_id": report.report_id,
                    }
                    sources.append(self._report_source(report, run, symbol))
                return (
                    rows,
                    {
                        "state": "AVAILABLE" if rows else "MISSING",
                        "reason_code": "OK" if rows else "PUBLISHED_RESEARCH_NOT_FOUND",
                        "symbols": statuses,
                    },
                    sources,
                )

            item = session.execute(
                select(ReportRow, JobRun)
                .join(JobRun, JobRun.run_id == ReportRow.run_id)
                .where(
                    JobRun.user_id == user_id,
                    JobRun.status.in_(("SUCCEEDED", "FUSED")),
                    JobRun.decision_at <= decision_at,
                    JobRun.completed_at.is_not(None),
                    JobRun.completed_at <= decision_at,
                    ReportRow.created_at <= decision_at,
                    ReportRow.report_type == "DAILY_RESEARCH",
                )
                .order_by(
                    JobRun.trading_date.desc(),
                    JobRun.completed_at.desc(),
                    ReportRow.created_at.desc(),
                )
                .limit(1)
            ).first()
            if item is None:
                return (
                    {},
                    {"state": "MISSING", "reason_code": "PUBLISHED_RESEARCH_NOT_FOUND"},
                    [],
                )
            report, run = item
            candidates = list(
                session.scalars(
                    select(CandidateRow)
                    .where(CandidateRow.run_id == run.run_id)
                    .order_by(CandidateRow.rank)
                    .limit(3)
                ).all()
            )
            names = self._security_names(
                session, [row.symbol for row in candidates], run.decision_at
            )
            manifest = dict(run.manifest or {})
            gate = manifest.get("data_quality_gate")
            gate = gate if isinstance(gate, dict) else {}
            risk = manifest.get("risk_outcome")
            risk = risk if isinstance(risk, dict) else {}
            return (
                {
                    "market_summary": {
                        "trading_date": run.trading_date.isoformat(),
                        "run_status": run.status,
                        "formal_eligible_count": len(gate.get("formal_eligible_symbols", [])),
                        "excluded_count": len(
                            gate.get("excluded_symbols", {})
                            if isinstance(gate.get("excluded_symbols"), dict)
                            else {}
                        ),
                        "risk": {
                            "reason_code": risk.get("reason_code"),
                            "reason_message": risk.get("reason_message"),
                        },
                        "report": self._report_reference(report, run),
                    },
                    "candidate_overview": [
                        {
                            "symbol": row.symbol,
                            "name": names.get(row.symbol),
                            "rank": row.rank,
                            "industry_code": row.industry_code,
                            "prediction_percentile": row.prediction_percentile,
                        }
                        for row in candidates
                    ],
                },
                {"state": "AVAILABLE", "reason_code": "OK", "report_id": report.report_id},
                [self._report_source(report, run)],
            )

    @staticmethod
    def _security_names(session: Any, symbols: list[str], decision_at: datetime) -> dict[str, str]:
        if not symbols:
            return {}
        rows = session.scalars(
            select(SecurityMaster)
            .where(
                SecurityMaster.symbol.in_(symbols),
                SecurityMaster.available_at <= decision_at,
                SecurityMaster.effective_from <= decision_at.date(),
                or_(
                    SecurityMaster.effective_to.is_(None),
                    SecurityMaster.effective_to >= decision_at.date(),
                ),
            )
            .order_by(
                SecurityMaster.symbol,
                SecurityMaster.available_at.desc(),
                SecurityMaster.fetched_at.desc(),
            )
        ).all()
        names: dict[str, str] = {}
        for row in rows:
            names.setdefault(row.symbol, row.short_name)
        return names

    @staticmethod
    def _report_reference(report: ReportRow, run: JobRun) -> dict[str, str]:
        return {
            "report_id": report.report_id,
            "trading_date": run.trading_date.isoformat(),
            "decision_at": run.decision_at.isoformat(),
            "uri": f"/api/v1/reports/{report.report_id}/symbols",
        }

    def _report_source(
        self, report: ReportRow, run: JobRun, symbol: str | None = None
    ) -> dict[str, Any]:
        source = {
            "source": "published_research",
            "title": f"{run.trading_date.isoformat()} 已发布研究报告",
            "uri": f"/api/v1/reports/{report.report_id}/symbols",
            "available_at": report.created_at.isoformat(),
        }
        if symbol is not None:
            source["symbol"] = symbol
        return source

    def _published_symbol_research_entry(
        self,
        score: ScoreRow,
        candidate: CandidateRow | None,
        run: JobRun,
        report: ReportRow,
    ) -> dict[str, Any]:
        manifest = dict(run.manifest or {})
        gate = manifest.get("data_quality_gate")
        gate = gate if isinstance(gate, dict) else {}
        declared = "formal_eligible_symbols" in gate
        eligible = {str(item) for item in gate.get("formal_eligible_symbols", [])}
        raw_excluded = gate.get("excluded_symbols")
        excluded = raw_excluded if isinstance(raw_excluded, dict) else {}
        reasons = [str(item) for item in excluded.get(score.symbol, [])]
        global_fused = run.status == "FUSED"
        if global_fused:
            risk = manifest.get("risk_outcome")
            risk = risk if isinstance(risk, dict) else {}
            reasons = list(
                dict.fromkeys([*reasons, str(risk.get("reason_code") or "GLOBAL_RISK_FUSE_ACTIVE")])
            )
        elif score.event_risk_multiplier <= 0:
            reasons = list(dict.fromkeys([*reasons, "CRITICAL_EVENT_RISK"]))
        advice_eligible = (
            not global_fused
            and (score.symbol in eligible if declared else candidate is not None)
            and score.event_risk_multiplier > 0
            and candidate is not None
        )
        dimensions = {
            "fundamental": score.fundamental_score,
            "technical": score.technical_score,
            "sentiment": score.sentiment_score,
        }
        return {
            "ranking": {
                "rank": candidate.rank if candidate else None,
                "prediction_percentile": candidate.prediction_percentile if candidate else None,
            },
            "gate": {
                "research_status": (
                    "FORMAL"
                    if advice_eligible
                    else "RISK_BLOCKED"
                    if global_fused or score.event_risk_multiplier <= 0
                    else "FORMAL_WITH_LIMITATIONS"
                ),
                "advice_eligible": advice_eligible,
                "exclusion_reasons": reasons,
            },
            "risk": {
                "event_risk_multiplier": score.event_risk_multiplier,
                "conclusion": (
                    "GLOBAL_FUSED"
                    if global_fused
                    else "CRITICAL_EVENT_RISK"
                    if score.event_risk_multiplier <= 0
                    else "NORMAL"
                ),
            },
            "brief": {
                "summary": symbol_summary(
                    total_score=score.total_score,
                    fundamental_score=score.fundamental_score,
                    technical_score=score.technical_score,
                    sentiment_score=score.sentiment_score,
                    advice_eligible=advice_eligible,
                    reasons=reasons,
                ),
                "strongest_dimension": max(dimensions, key=dimensions.__getitem__),
                "weakest_dimension": min(dimensions, key=dimensions.__getitem__),
            },
            "report": self._report_reference(report, run),
        }

    def _historical_daily_bars(
        self,
        *,
        user_id: str,
        symbol: str,
        decision_at: datetime,
    ) -> dict[str, Any]:
        """Read only a committed immutable bundle for an explicit historical turn."""

        with self.session_factory() as session:
            manifest = session.scalar(
                select(SnapshotManifestRow)
                .join(JobRun, JobRun.run_id == SnapshotManifestRow.run_id)
                .where(
                    SnapshotManifestRow.dataset == "backtest_bundle",
                    SnapshotManifestRow.status == "COMMITTED",
                    SnapshotManifestRow.fetched_at <= decision_at,
                    SnapshotManifestRow.committed_at.is_not(None),
                    SnapshotManifestRow.committed_at <= decision_at,
                    JobRun.user_id == user_id,
                    JobRun.status.in_(("SUCCEEDED", "FUSED")),
                    JobRun.completed_at.is_not(None),
                    JobRun.completed_at <= decision_at,
                )
                .order_by(
                    SnapshotManifestRow.fetched_at.desc(),
                    SnapshotManifestRow.committed_at.desc(),
                )
                .limit(1)
            )
            if manifest is None:
                return {
                    "bars": [],
                    "status": {
                        "state": "MISSING",
                        "reason_code": "PIT_KLINE_MANIFEST_NOT_FOUND",
                        "source": "committed_manifest",
                    },
                }
            expected_hash = str((manifest.details or {}).get("parquet_file_sha256") or "")
            snapshot_id = manifest.snapshot_id
            snapshot_uri = manifest.parquet_uri
        if len(expected_hash) != 64:
            return {
                "bars": [],
                "status": {
                    "state": "UNAVAILABLE",
                    "reason_code": "PIT_KLINE_MANIFEST_INVALID",
                    "source": "committed_manifest",
                },
            }
        try:
            from ashare_ai.orchestration.builtin_backtest import read_backtest_bundle

            bundle = read_backtest_bundle({snapshot_id: snapshot_uri}, {snapshot_id: expected_hash})
        except Exception:
            return {
                "bars": [],
                "status": {
                    "state": "UNAVAILABLE",
                    "reason_code": "PIT_KLINE_MANIFEST_UNAVAILABLE",
                    "source": "committed_manifest",
                },
            }
        selected = [
            item
            for item in bundle.bars
            if item.symbol == symbol and item.available_at <= decision_at
        ]
        bars = [
            {
                "timestamp": datetime.combine(
                    item.trading_date,
                    datetime.min.time(),
                    tzinfo=item.available_at.tzinfo,
                ).isoformat(),
                "open": float(item.open),
                "high": float(item.high),
                "low": float(item.low),
                "close": float(item.close),
                "volume": float(item.volume),
                "amount": float(item.amount),
            }
            for item in sorted(selected, key=lambda item: item.trading_date)[-30:]
        ]
        available_at = max((item.available_at for item in selected), default=None)
        return {
            "bars": bars,
            "status": {
                "state": "AVAILABLE" if bars else "MISSING",
                "reason_code": "OK" if bars else "PIT_KLINE_NOT_FOUND",
                "source": "committed_manifest",
                "available_at": available_at.isoformat() if available_at else None,
                "snapshot_id": snapshot_id,
            },
        }

    def _minimal_unavailable_context(
        self, *, symbols: list[str], requested_decision_at: datetime | None, wait_ms: int
    ) -> ChatContextResult:
        decision_at = requested_decision_at or datetime.now(UTC)
        statuses = {
            "positions": {"state": "UNAVAILABLE", "reason_code": "CONTEXT_SINGLEFLIGHT_TIMEOUT"},
            "scores": {},
            "quotes": {},
            "daily_bars": {},
            "news": {},
            "latest_published_research": {
                "state": "UNAVAILABLE",
                "reason_code": "CONTEXT_SINGLEFLIGHT_TIMEOUT",
            },
        }
        return ChatContextResult(
            context={
                "symbols": symbols,
                "positions": {},
                "quotes": {},
                "daily_bars": {},
                "latest_formal_scores": {},
                "latest_published_research": {},
                "news": {},
                "decision_at": decision_at.isoformat(),
                "historical": requested_decision_at is not None,
                "data_status": statuses,
            },
            sources=[],
            decision_at=decision_at,
            data_status=statuses,
            context_cache_hit=False,
            market_cache_hit=False,
            news_cache_hit=False,
            singleflight_wait_ms=wait_ms,
        )


def _with_context_cache_hit(result: ChatContextResult, *, wait_ms: int) -> ChatContextResult:
    copied = copy.deepcopy(result.context)
    return ChatContextResult(
        context=copied,
        sources=copy.deepcopy(result.sources),
        decision_at=result.decision_at,
        data_status=copy.deepcopy(result.data_status),
        context_cache_hit=True,
        market_cache_hit=result.market_cache_hit,
        news_cache_hit=result.news_cache_hit,
        singleflight_wait_ms=wait_ms,
    )


def _chat_policy(settings: Settings) -> dict[str, Any]:
    try:
        raw = json.loads(Path(settings.policy_config_path).read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    return raw.get("chat", {}) if isinstance(raw, dict) else {}


def _positive_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(high, max(low, parsed))


def _live_bucket(cache_seconds: int) -> int:
    return int(time.time() // cache_seconds)


@lru_cache(maxsize=1)
def get_chat_context_service() -> ChatContextService:
    return ChatContextService()
