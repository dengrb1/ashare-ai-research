from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ashare_ai.agents.chat_context import ChatContextService, resolve_security_mentions
from ashare_ai.search.news import NewsSearchResult
from ashare_ai.storage.models import (
    Base,
    JobRun,
    SecurityMaster,
    SnapshotManifestRow,
    UserAccount,
)


def _engine():
    return create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def _master(
    *, symbol: str, name: str, available_at: datetime, source_record_id: str
) -> SecurityMaster:
    code, exchange = symbol.split(".", 1)
    return SecurityMaster(
        symbol=symbol,
        trading_date=available_at.date(),
        exchange=exchange,
        board="MAIN",
        short_name=name,
        list_date=date(2010, 1, 1),
        effective_from=date(2010, 1, 1),
        effective_to=None,
        is_st=False,
        is_suspended=False,
        source="fixture",
        source_record_id=source_record_id,
        available_at=available_at,
        fetched_at=available_at,
        payload_sha256=(code * 11)[:64].ljust(64, "0"),
        schema_version="1",
        adapter_version="fixture",
        ingestion_run_id="fixture-run",
        availability_basis="PUBLISHED",
    )


def test_resolve_security_mentions_uses_master_for_name_and_six_digit_code() -> None:
    engine = _engine()
    Base.metadata.create_all(engine)
    as_of = datetime(2026, 7, 20, 8, tzinfo=UTC)
    with Session(engine) as session:
        session.add(
            _master(
                symbol="002138.SZ",
                name="顺络电子",
                available_at=as_of - timedelta(days=1),
                source_record_id="master-002138",
            )
        )
        session.commit()

    def factory() -> Session:
        return Session(engine)

    resolved = resolve_security_mentions(
        "请比较 @顺络电子 和 @002138", [], decision_at=as_of, session_factory=factory
    )

    assert resolved.refs == [{"symbol": "002138.SZ", "name": "顺络电子"}]
    assert [item["state"] for item in resolved.statuses] == ["RESOLVED", "RESOLVED"]

    with Session(engine) as session:
        session.add(
            _master(
                symbol="002139.SZ",
                name="顺络电子",
                available_at=as_of - timedelta(days=1),
                source_record_id="master-002139",
            )
        )
        session.commit()

    ambiguous = resolve_security_mentions(
        "请看 @顺络电子", [], decision_at=as_of, session_factory=factory
    )

    assert ambiguous.refs == []
    assert ambiguous.statuses == [
        {
            "mention": "顺络电子",
            "state": "AMBIGUOUS",
            "reason_code": "SECURITY_NAME_AMBIGUOUS",
        }
    ]


class _Market:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.quote_calls = 0
        self.kline_calls = 0

    def quotes(self, symbols: list[str]) -> list[dict[str, object]]:
        self.quote_calls += 1
        return [
            {
                "symbol": symbol,
                "price": 31.5,
                "status": {
                    "source": "fixture-market",
                    "collected_at": self.now.isoformat(),
                    "cache_hit": False,
                },
            }
            for symbol in symbols
        ]

    def klines(
        self, symbol: str, period: str, *, limit: int, end: datetime
    ) -> dict[str, object]:
        assert period == "day"
        assert limit == 30
        assert end.tzinfo is not None
        self.kline_calls += 1
        return {
            "bars": [
                {
                    "timestamp": "2026-07-17T00:00:00+08:00",
                    "open": 30.0,
                    "high": 32.0,
                    "low": 29.0,
                    "close": 31.5,
                    "volume": 1000.0,
                    "amount": 31500.0,
                }
            ],
            "status": {
                "source": "fixture-market",
                "collected_at": self.now.isoformat(),
                "cache_hit": False,
            },
        }


class _News:
    def __init__(self) -> None:
        self.calls = 0

    def search_for_security(self, **_: object) -> NewsSearchResult:
        self.calls += 1
        return NewsSearchResult(
            items=[
                {
                    "title": "顺络电子公告",
                    "url": "https://example.test/news",
                    "snippet": "fixture",
                    "engine": "fixture",
                    "published_at": "2026-07-19T00:00:00+08:00",
                }
            ],
            status={"state": "AVAILABLE", "reason_code": "OK", "source": "searxng"},
            cache_hit=False,
        )


def test_live_context_contains_market_kline_news_and_reuses_user_cache() -> None:
    engine = _engine()
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 20, 8, tzinfo=UTC)
    with Session(engine) as session:
        session.add(
            UserAccount(
                user_id="chat-user",
                username="chat-user",
                password_hash="hash",
                role="USER",
                enabled=True,
                session_version=1,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()
    market = _Market(now)
    news = _News()
    service = ChatContextService(
        market=market,
        news=news,  # type: ignore[arg-type]
        session_factory=lambda: Session(engine),
    )
    refs = [{"symbol": "002138.SZ", "name": "顺络电子"}]

    first = service.build(
        user_id="chat-user",
        refs=refs,
        requested_decision_at=None,
        web_search=True,
        model_configuration_sha256="a" * 64,
    )
    second = service.build(
        user_id="chat-user",
        refs=refs,
        requested_decision_at=None,
        web_search=True,
        model_configuration_sha256="a" * 64,
    )

    assert first.context["quotes"]["002138.SZ"]["price"] == 31.5
    assert first.context["daily_bars"]["002138.SZ"]
    assert first.context["news"]["002138.SZ"][0]["title"] == "顺络电子公告"
    assert first.data_status["quotes"]["002138.SZ"]["state"] == "AVAILABLE"
    assert second.context_cache_hit is True
    assert market.quote_calls == market.kline_calls == news.calls == 1


def test_historical_bars_require_a_committed_user_manifest_before_decision(
    monkeypatch
) -> None:
    engine = _engine()
    Base.metadata.create_all(engine)
    decision_at = datetime(2026, 7, 20, 8, tzinfo=UTC)
    with Session(engine) as session:
        session.add(
            UserAccount(
                user_id="history-user",
                username="history-user",
                password_hash="hash",
                role="USER",
                enabled=True,
                session_version=1,
                created_at=decision_at - timedelta(days=2),
                updated_at=decision_at - timedelta(days=2),
            )
        )
        session.add(
            JobRun(
                run_id="history-run",
                user_id="history-user",
                run_type="BACKTEST",
                trading_date=decision_at.date(),
                decision_at=decision_at - timedelta(days=1),
                status="SUCCEEDED",
                idempotency_key="history-run-key",
                manifest={},
                input_hash="b" * 64,
                started_at=decision_at - timedelta(days=1),
                completed_at=decision_at + timedelta(minutes=1),
            )
        )
        session.add(
            SnapshotManifestRow(
                snapshot_id="history-snapshot",
                run_id="history-run",
                dataset="backtest_bundle",
                source="fixture",
                schema_version="1",
                adapter_version="fixture",
                fetched_at=decision_at - timedelta(days=1),
                row_count=1,
                payload_sha256="c" * 64,
                parquet_uri="file:///fixture.parquet",
                status="COMMITTED",
                details={"parquet_file_sha256": "d" * 64},
                committed_at=decision_at + timedelta(minutes=1),
            )
        )
        session.commit()

    service = ChatContextService(
        market=object(),
        news=object(),  # type: ignore[arg-type]
        session_factory=lambda: Session(engine),
    )
    from ashare_ai.orchestration import builtin_backtest

    calls: list[object] = []
    bar = SimpleNamespace(
        symbol="002138.SZ",
        trading_date=date(2026, 7, 17),
        available_at=decision_at - timedelta(days=1),
        open=Decimal("30"),
        high=Decimal("32"),
        low=Decimal("29"),
        close=Decimal("31"),
        volume=1000,
        amount=Decimal("31000"),
    )
    monkeypatch.setattr(
        builtin_backtest,
        "read_backtest_bundle",
        lambda *_: calls.append("read") or SimpleNamespace(bars=(bar,)),
    )

    unavailable = service._historical_daily_bars(
        user_id="history-user", symbol="002138.SZ", decision_at=decision_at
    )
    assert unavailable["status"]["reason_code"] == "PIT_KLINE_MANIFEST_NOT_FOUND"
    assert calls == []

    with Session(engine) as session:
        run = session.get(JobRun, "history-run")
        manifest = session.get(SnapshotManifestRow, "history-snapshot")
        assert run is not None and manifest is not None
        run.completed_at = decision_at - timedelta(minutes=1)
        manifest.committed_at = decision_at - timedelta(minutes=1)
        session.commit()

    available = service._historical_daily_bars(
        user_id="history-user", symbol="002138.SZ", decision_at=decision_at
    )
    assert available["status"]["state"] == "AVAILABLE"
    assert available["bars"][0]["close"] == 31.0
    assert calls == ["read"]
