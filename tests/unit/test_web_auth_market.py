from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ashare_ai.api.app import app
from ashare_ai.api.auth import hash_password
from ashare_ai.api.dependencies import get_db
from ashare_ai.core.config import Settings
from ashare_ai.market.service import MarketDataService
from ashare_ai.storage.models import (
    BacktestRun,
    Base,
    JobRun,
    SnapshotManifestRow,
    UserAccount,
)


class QuoteProvider:
    source = "akshare"

    def __init__(self) -> None:
        self.calls = 0
        self.fail = False

    def quotes(self, symbols):
        del symbols
        self.calls += 1
        if self.fail:
            raise RuntimeError("offline")
        return [
            {"symbol": "600000.SH", "price": 10.0},
            {"symbol": "000001.SZ", "price": 12.0},
        ]

    def klines(self, symbol, period, start, end, limit):
        del symbol, period, start, end, limit
        return []


class SlowQuoteProvider(QuoteProvider):
    def quotes(self, symbols):
        time.sleep(0.05)
        return super().quotes(symbols)


class FailingProvider(QuoteProvider):
    source = "akshare"

    def quotes(self, symbols):
        del symbols
        raise RuntimeError("primary unavailable")

    def klines(self, symbol, period, start, end, limit):
        del symbol, period, start, end, limit
        raise RuntimeError("primary unavailable")


class FallbackProvider(QuoteProvider):
    source = "tushare"

    def klines(self, symbol, period, start, end, limit):
        del symbol, start, end, limit
        assert period == "daily"
        return [
            {
                "timestamp": "2026-07-14T00:00:00+08:00",
                "open": 10,
                "high": 11,
                "low": 9,
                "close": 10.5,
                "volume": 100,
                "amount": 1000,
                "turnover_rate": 1.25,
            }
        ]


class PartialProvider(QuoteProvider):
    def quotes(self, symbols):
        del symbols
        self.calls += 1
        return [{"symbol": "600000.SH", "price": 10.0}]


class SymbolFallback(FallbackProvider):
    def quotes(self, symbols):
        self.calls += 1
        return [{"symbol": symbol, "price": 20.0} for symbol in symbols if symbol == "000001.SZ"]


class EmptyFallback:
    source = "empty"

    def quotes(self, symbols):
        del symbols
        return []

    def klines(self, symbol, period, start, end, limit):
        del symbol, period, start, end, limit
        return []


class PrefetchProvider(QuoteProvider):
    def __init__(self, failing_symbol: str | None = None) -> None:
        super().__init__()
        self.failing_symbol = failing_symbol
        self.kline_calls: dict[str, int] = {}
        self.active = 0
        self.max_active = 0
        self._active_lock = threading.Lock()

    def _enter(self) -> None:
        with self._active_lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)

    def _leave(self) -> None:
        with self._active_lock:
            self.active -= 1

    def quotes(self, symbols):
        self._enter()
        try:
            time.sleep(0.02)
            self.calls += 1
            return [{"symbol": symbol, "price": 10.0} for symbol in symbols]
        finally:
            self._leave()

    def klines(self, symbol, period, start, end, limit):
        del start, end, limit
        self._enter()
        try:
            time.sleep(0.02)
            self.kline_calls[symbol] = self.kline_calls.get(symbol, 0) + 1
            if symbol == self.failing_symbol:
                raise RuntimeError("symbol unavailable")
            assert period == "daily"
            return [
                {
                    "timestamp": "2026-07-14T00:00:00+08:00",
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10.5,
                    "volume": 100,
                    "amount": 1000,
                }
            ]
        finally:
            self._leave()

class SharedRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            return self.values.get(key)

    def set(self, key, value, nx=False, ex=None):
        del ex
        with self._lock:
            if nx and key in self.values:
                return False
            self.values[key] = value
            return True

    def setex(self, key, seconds, value):
        del seconds
        with self._lock:
            self.values[key] = value

    def eval(self, script, number, key, token):
        del script, number
        with self._lock:
            if self.values.get(key) == token:
                self.values.pop(key, None)
                return 1
            return 0


class PreparedPipeline:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.start_calls = 0

    def start_run(self, trading_date: date) -> str:
        self.start_calls += 1
        now = datetime.now(UTC)
        self.session.add(
            JobRun(
                run_id="prepared-run",
                run_type="DAILY",
                trading_date=trading_date,
                decision_at=now,
                status="RUNNING",
                idempotency_key="prepared-key",
                manifest={
                    "code_git_sha": "abc",
                    "policy_config_sha256": "p" * 64,
                    "dependency_lock_sha256": "d" * 64,
                    "formula_version": "v1",
                },
                input_hash="i" * 64,
                started_at=now,
            )
        )
        self.session.commit()
        return "prepared-run"


def _database() -> tuple[Session, UserAccount, UserAccount]:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    admin = UserAccount(
        username="admin",
        password_hash=hash_password("admin-password"),
        role="ADMIN",
        enabled=True,
        created_at=now,
        updated_at=now,
    )
    user = UserAccount(
        username="alice",
        password_hash=hash_password("alice-password"),
        role="USER",
        enabled=True,
        created_at=now,
        updated_at=now,
    )
    session.add_all([admin, user])
    session.commit()
    return session, admin, user


def test_login_csrf_admin_and_disabled_session_invalidation() -> None:
    session, admin, user = _database()

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    try:
        anonymous = TestClient(app)
        assert anonymous.get("/api/v1/runs").status_code == 401
        client = TestClient(app)
        assert (
            client.post(
                "/api/v1/auth/login",
                json={"username": "alice", "password": "alice-password"},
            ).status_code
            == 200
        )
        assert (
            client.post("/api/v1/research/runs", json={"trading_date": "2026-07-14"}).status_code
            == 403
        )

        admin_client = TestClient(app)
        assert (
            admin_client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": "admin-password"},
            ).status_code
            == 200
        )
        csrf = admin_client.cookies.get("ashare_csrf")
        changed = admin_client.patch(
            f"/api/v1/admin/users/{user.user_id}",
            json={"enabled": False},
            headers={"x-csrf-token": csrf},
        )
        assert changed.status_code == 200
        assert client.get("/api/v1/auth/me").status_code == 401
        assert admin.user_id != user.user_id
    finally:
        app.dependency_overrides.clear()
        session.close()


def test_market_kline_api_forwards_segment_boundaries_and_refresh(monkeypatch) -> None:
    session, _, _ = _database()
    calls: list[dict[str, object]] = []

    class StubMarket:
        def klines(self, symbol, period, *, limit, start, end, force_refresh):
            calls.append(
                {
                    "symbol": symbol,
                    "period": period,
                    "limit": limit,
                    "start": start,
                    "end": end,
                    "force_refresh": force_refresh,
                }
            )
            return {
                "symbol": symbol,
                "period": period,
                "adjustment": "hfq",
                "bars": [
                    {
                        "timestamp": "2026-07-15T09:31:00+08:00",
                        "open": 10,
                        "high": 11,
                        "low": 9,
                        "close": 10.5,
                        "volume": 100,
                        "amount": None,
                    }
                ],
                "status": {
                    "source": "fixture",
                    "collected_at": "2026-07-17T12:00:00+08:00",
                    "cached_at": "2026-07-17T12:00:00+08:00",
                    "delayed": False,
                    "stale": False,
                },
            }

    def override_db():
        yield session

    monkeypatch.setattr("ashare_ai.api.app.get_market_data_service", lambda: StubMarket())
    app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(app)
        client.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": "alice-password"},
        )
        response = client.get(
            "/api/v1/market/klines/600519.SH",
            params={
                "period": "1m",
                "limit": 1200,
                "adjust": "hfq",
                "start": "2026-07-10T09:30:00+08:00",
                "end": "2026-07-17T15:00:00+08:00",
                "refresh": "true",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["bars"][0]["amount"] is None
        assert calls == [
            {
                "symbol": "600519.SH",
                "period": "1m",
                "limit": 1200,
                "start": datetime.fromisoformat("2026-07-10T09:30:00+08:00"),
                "end": datetime.fromisoformat("2026-07-17T15:00:00+08:00"),
                "force_refresh": True,
            }
        ]
        assert client.get(
            "/api/v1/market/klines/600519.SH", params={"adjust": "qfq"}
        ).status_code == 422
    finally:
        app.dependency_overrides.clear()
        session.close()


def test_user_scoped_latest_run_selection() -> None:
    session, _, user = _database()
    other = session.scalar(select(UserAccount).where(UserAccount.username == "admin"))
    assert other is not None
    now = datetime.now(UTC)
    session.add_all(
        [
            JobRun(
                run_id="alice-run",
                user_id=user.user_id,
                run_type="DAILY",
                trading_date=date(2026, 7, 14),
                decision_at=now,
                status="SUCCEEDED",
                idempotency_key="alice-key",
                manifest={},
                input_hash="a" * 64,
                started_at=now,
                completed_at=now,
            ),
            JobRun(
                run_id="other-run",
                user_id=other.user_id,
                run_type="DAILY",
                trading_date=date(2026, 7, 14),
                decision_at=now + timedelta(minutes=1),
                status="SUCCEEDED",
                idempotency_key="other-key",
                manifest={},
                input_hash="b" * 64,
                started_at=now,
                completed_at=now + timedelta(minutes=1),
            ),
        ]
    )
    session.commit()

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(app)
        client.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": "alice-password"},
        )
        assert client.get("/api/v1/runs/other-run").status_code == 404
        assert [item["run_id"] for item in client.get("/api/v1/runs").json()] == ["alice-run"]
    finally:
        app.dependency_overrides.clear()
        session.close()


def test_quote_cache_is_shared_across_disjoint_symbol_sets_and_degrades_stale() -> None:
    provider = QuoteProvider()
    current = [datetime(2026, 7, 15, 2, tzinfo=UTC)]
    settings = Settings(
        redis_url="redis://127.0.0.1:1/0",
        market_cache_seconds=15,
        market_stale_seconds=60,
        market_timeout_seconds=1,
    )
    service = MarketDataService(
        primary=provider,
        fallback=EmptyFallback(),
        settings=settings,
        clock=lambda: current[0],
    )
    assert service.quotes(["600000.SH"])[0]["price"] == 10
    assert service.quotes(["000001.SZ"])[0]["price"] == 12
    assert provider.calls == 1
    current[0] += timedelta(seconds=16)
    provider.fail = True
    stale = service.quotes(["600000.SH"])
    assert stale[0]["status"]["delayed"] is True
    assert stale[0]["status"]["stale"] is True
    assert provider.calls == 2


def test_quote_cache_older_than_stale_limit_is_not_reused() -> None:
    provider = QuoteProvider()
    current = [datetime(2026, 7, 15, 2, tzinfo=UTC)]
    settings = Settings(
        market_cache_seconds=15,
        market_stale_seconds=60,
        market_timeout_seconds=1,
    )
    service = MarketDataService(
        primary=provider,
        fallback=EmptyFallback(),
        settings=settings,
        clock=lambda: current[0],
        redis_client=None,
    )
    assert service.quotes(["600000.SH"])[0]["price"] == 10
    current[0] += timedelta(seconds=61)
    provider.fail = True
    with pytest.raises(RuntimeError, match="market data missing"):
        service.quotes(["600000.SH"])


def test_concurrent_disjoint_quote_requests_are_coalesced() -> None:
    provider = SlowQuoteProvider()
    settings = Settings(
        redis_url="redis://127.0.0.1:1/0",
        market_cache_seconds=15,
        market_stale_seconds=60,
        market_timeout_seconds=1,
    )
    service = MarketDataService(primary=provider, settings=settings)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.quotes, ["600000.SH"])
        second = pool.submit(service.quotes, ["000001.SZ"])
        assert first.result()[0]["symbol"] == "600000.SH"
        assert second.result()[0]["symbol"] == "000001.SZ"
    assert provider.calls == 1


def test_tushare_fallback_is_labeled_and_hfq_kline_contract_is_preserved() -> None:
    settings = Settings(
        redis_url="redis://127.0.0.1:1/0",
        market_cache_seconds=15,
        market_stale_seconds=60,
        market_timeout_seconds=1,
    )
    fallback = FallbackProvider()
    service = MarketDataService(
        primary=FailingProvider(),
        fallback=fallback,
        settings=settings,
    )
    quote = service.quotes(["600000.SH"])[0]
    assert quote["status"]["source"] == "tushare"
    assert quote["status"]["delayed"] is True
    kline = service.klines("600000.SH", "day", limit=1)
    assert kline["adjustment"] == "hfq"
    assert kline["status"]["source"] == "tushare"
    assert kline["bars"][0]["close"] == 10.5
    assert kline["bars"][0]["turnover_rate"] == 1.25


def test_missing_primary_quote_is_filled_by_tushare_without_dropping_primary_data() -> None:
    settings = Settings(
        redis_url="redis://127.0.0.1:1/0",
        market_cache_seconds=15,
        market_stale_seconds=60,
        market_timeout_seconds=1,
    )
    primary = PartialProvider()
    fallback = SymbolFallback()
    rows = MarketDataService(
        primary=primary,
        fallback=fallback,
        settings=settings,
    ).quotes(["600000.SH", "000001.SZ"])
    by_symbol = {item["symbol"]: item for item in rows}
    assert by_symbol["600000.SH"]["status"]["source"] == "akshare"
    assert by_symbol["000001.SZ"]["status"]["source"] == "tushare"
    assert primary.calls == fallback.calls == 1


def test_default_free_fallback_recovers_quotes_and_klines(monkeypatch) -> None:
    class Response:
        def __init__(self, content: bytes, payload=None) -> None:
            self.content = content
            self.payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self):
            return self.payload

    def fake_get(url: str, **kwargs):
        del kwargs
        if url.startswith("https://hq.sinajs.cn/list="):
            return Response(
                (
                    'var hq_str_sh600519="贵州茅台,1252.000,1251.060,1258.990,'
                    '1267.970,1245.050,0,0,4761114,5987570858.000";'
                ).encode("gb18030")
            )
        if url.startswith("https://quotes.sina.cn/"):
            return Response(
                b"[]",
                [
                    {
                        "day": "2026-07-16 09:30:00",
                        "open": "1252.00",
                        "high": "1259.00",
                        "low": "1250.00",
                        "close": "1258.99",
                        "volume": "100",
                        "amount": "125899",
                    },
                    {
                        "day": "2026-07-16 09:35:00",
                        "open": "1258.99",
                        "high": "1260.00",
                        "low": "1258.00",
                        "close": "1259.50",
                        "volume": "200",
                        "amount": "251900",
                    },
                ],
            )
        if url.startswith("https://proxy.finance.qq.com/"):
            return Response(
                b'kline_dayhfq2026={"code":0,"data":{"sh600519":{"hfqday":'
                b'[["2026-07-15","1200","1210","1215","1190","500",{},'
                b'"0.1","605000"]]}}};'
            )
        raise AssertionError(f"unexpected request: {url}")

    monkeypatch.setattr("ashare_ai.market.service.httpx.get", fake_get)
    settings = Settings(
        market_cache_seconds=15,
        market_stale_seconds=60,
        market_timeout_seconds=1,
    )
    service = MarketDataService(
        primary=FailingProvider(), settings=settings, redis_client=None
    )

    quote = service.quotes(["600519.SH"])[0]
    assert quote["name"] == "贵州茅台"
    assert quote["price"] == 1258.99
    assert quote["status"]["source"] == "sina"
    assert quote["status"]["delayed"] is False

    minute = service.klines("600519.SH", "5m", limit=2)
    assert minute["status"]["source"] == "sina"
    assert minute["status"]["delayed"] is False
    assert [bar["close"] for bar in minute["bars"]] == [1258.99, 1259.5]

    daily = service.klines(
        "600519.SH",
        "day",
        limit=1,
        start=datetime(2026, 7, 1),
        end=datetime(2026, 7, 16, 23, 59),
    )
    assert daily["adjustment"] == "hfq"
    assert daily["status"]["source"] == "tencent"
    assert daily["bars"][0]["close"] == 1210.0


def test_kline_refresh_is_single_flight_across_service_instances() -> None:
    settings = Settings(
        market_cache_seconds=15,
        market_stale_seconds=60,
        market_timeout_seconds=1,
    )
    shared = SharedRedis()

    class SlowKline(QuoteProvider):
        def klines(self, symbol, period, start, end, limit):
            del symbol, period, start, end, limit
            self.calls += 1
            time.sleep(0.05)
            return [
                {
                    "timestamp": "2026-07-14T00:00:00+08:00",
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10.5,
                    "volume": 100,
                    "amount": 1000,
                }
            ]

    provider = SlowKline()
    first_service = MarketDataService(
        primary=provider,
        settings=settings,
        redis_client=shared,
    )
    second_service = MarketDataService(
        primary=provider,
        settings=settings,
        redis_client=shared,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(first_service.klines, "600000.SH", "day", limit=1)
        second = pool.submit(second_service.klines, "600000.SH", "day", limit=1)
        assert first.result()["bars"]
        assert second.result()["bars"]
    assert provider.calls == 1


def test_prefetch_normalizes_symbols_limits_concurrency_and_isolates_failures() -> None:
    provider = PrefetchProvider(failing_symbol="300750.SZ")
    settings = Settings(
        market_cache_seconds=15,
        market_kline_cache_seconds=300,
        market_prefetch_max_workers=2,
        market_stale_seconds=900,
        market_timeout_seconds=1,
    )
    service = MarketDataService(
        primary=provider,
        fallback=EmptyFallback(),
        settings=settings,
        redis_client=None,
    )

    payload = service.prefetch(
        ["600519", "600519.SH", "300750.sz", "000001.SZ"],
        periods=["day"],
        limit=160,
    )

    assert {quote["symbol"] for quote in payload["quotes"]} == {
        "000001.SZ",
        "300750.SZ",
        "600519.SH",
    }
    assert set(payload["klines"]) == {"000001.SZ", "600519.SH"}
    assert "symbol unavailable" in payload["errors"]["300750.SZ"]
    assert provider.max_active == 2
    with pytest.raises(ValueError, match="at most 50"):
        service.prefetch([f"{index:06d}.SZ" for index in range(1, 52)])
    with pytest.raises(ValueError, match="unsupported prefetch period"):
        service.prefetch(["600519.SH"], periods=["5m"])


def test_daily_kline_uses_independent_five_minute_cache() -> None:
    provider = PrefetchProvider()
    current = [datetime(2026, 7, 15, 2, tzinfo=UTC)]
    settings = Settings(
        market_cache_seconds=15,
        market_kline_cache_seconds=300,
        market_stale_seconds=900,
        market_timeout_seconds=1,
    )
    service = MarketDataService(
        primary=provider,
        fallback=EmptyFallback(),
        settings=settings,
        clock=lambda: current[0],
        redis_client=None,
    )

    service.klines("600519.SH", "day", limit=160)
    current[0] += timedelta(seconds=299)
    service.klines("600519.SH", "day", limit=160)
    assert provider.kline_calls["600519.SH"] == 1
    current[0] += timedelta(seconds=2)
    service.klines("600519.SH", "day", limit=160)
    assert provider.kline_calls["600519.SH"] == 2


def test_kline_timeout_degrades_to_recent_cache() -> None:
    class TimeoutProvider(PrefetchProvider):
        delay = 0.0

        def klines(self, symbol, period, start, end, limit):
            if self.delay:
                time.sleep(self.delay)
            return super().klines(symbol, period, start, end, limit)

    provider = TimeoutProvider()
    current = [datetime(2026, 7, 15, 2, tzinfo=UTC)]
    settings = Settings(
        market_cache_seconds=15,
        market_kline_cache_seconds=300,
        market_stale_seconds=900,
        market_timeout_seconds=0.1,
    )
    service = MarketDataService(
        primary=provider,
        fallback=EmptyFallback(),
        settings=settings,
        clock=lambda: current[0],
        redis_client=None,
    )
    service.klines("600519.SH", "day", limit=160)
    current[0] += timedelta(seconds=301)
    provider.delay = 0.2
    settings.market_timeout_seconds = 0.01

    stale = service.klines("600519.SH", "day", limit=160)

    assert stale["bars"]
    assert stale["status"]["delayed"] is True
    assert stale["status"]["stale"] is True
    assert "timeout" in stale["status"]["message"]


def test_prefetch_api_requires_auth_csrf_and_returns_indexed_results(monkeypatch) -> None:
    session, _, _ = _database()

    class StubMarket:
        def prefetch(self, symbols, *, periods, limit):
            if len(set(symbols)) > 50:
                raise ValueError("prefetch supports at most 50 symbols")
            if any(symbol == "INVALID" for symbol in symbols):
                raise ValueError("unsupported security symbol format")
            assert symbols == ["600519.SH"]
            assert periods == ["day"]
            assert limit == 160
            status = {
                "source": "fixture",
                "collected_at": "2026-07-16T01:00:00Z",
                "cached_at": "2026-07-16T01:00:01Z",
                "delayed": False,
                "stale": False,
            }
            return {
                "quotes": [{"symbol": "600519.SH", "price": 10, "status": status}],
                "klines": {
                    "600519.SH": {
                        "day": {
                            "symbol": "600519.SH",
                            "period": "day",
                            "adjustment": "hfq",
                            "bars": [
                                {
                                    "timestamp": "2026-07-15T00:00:00+08:00",
                                    "open": 10,
                                    "high": 11,
                                    "low": 9,
                                    "close": 10.5,
                                    "volume": 100,
                                }
                            ],
                            "status": status,
                        }
                    }
                },
                "errors": {},
            }

    def override_db():
        yield session

    monkeypatch.setattr("ashare_ai.api.app.get_market_data_service", lambda: StubMarket())
    app.dependency_overrides[get_db] = override_db
    try:
        anonymous = TestClient(app)
        assert anonymous.post(
            "/api/v1/market/prefetch", json={"symbols": ["600519.SH"]}
        ).status_code == 401
        client = TestClient(app)
        client.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": "alice-password"},
        )
        assert client.post(
            "/api/v1/market/prefetch", json={"symbols": ["600519.SH"]}
        ).status_code == 403
        csrf = client.cookies.get("ashare_csrf")
        response = client.post(
            "/api/v1/market/prefetch",
            json={"symbols": ["600519.SH"], "periods": ["day"], "limit": 160},
            headers={"x-csrf-token": csrf},
        )
        assert response.status_code == 200
        assert response.json()["klines"]["600519.SH"]["day"]["adjustment"] == "hfq"
        unsupported = client.post(
            "/api/v1/market/prefetch",
            json={"symbols": ["600519.SH"], "periods": ["5m"]},
            headers={"x-csrf-token": csrf},
        )
        assert unsupported.status_code == 422
        too_many = client.post(
            "/api/v1/market/prefetch",
            json={"symbols": [f"{index:06d}.SZ" for index in range(1, 52)]},
            headers={"x-csrf-token": csrf},
        )
        assert too_many.status_code == 422
        invalid = client.post(
            "/api/v1/market/prefetch",
            json={"symbols": ["INVALID"]},
            headers={"x-csrf-token": csrf},
        )
        assert invalid.status_code == 422
    finally:
        app.dependency_overrides.clear()
        session.close()


def test_research_submission_prepares_frozen_manifest_and_deduplicates(monkeypatch) -> None:
    session, _, user = _database()
    pipeline = PreparedPipeline(session)
    queued: list[str] = []
    monkeypatch.setattr("ashare_ai.api.app.load_pipeline", lambda: pipeline)
    monkeypatch.setattr("ashare_ai.api.app.enqueue_research", queued.append)

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(app)
        client.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": "alice-password"},
        )
        csrf = client.cookies.get("ashare_csrf")
        first = client.post(
            "/api/v1/research/runs",
            json={
                "trading_date": "2026-07-14",
                "scope": "CUSTOM",
                "symbols": ["600519.SH", "000858.SZ"],
                "total_budget": 1_000_000,
                "per_symbol_budget": 80_000,
                "max_stock_price": 500,
            },
            headers={"x-csrf-token": csrf},
        )
        second = client.post(
            "/api/v1/research/runs",
            json={
                "trading_date": "2026-07-14",
                "scope": "CUSTOM",
                "symbols": ["600519.SH", "000858.SZ"],
                "total_budget": 1_000_000,
                "per_symbol_budget": 80_000,
                "max_stock_price": 500,
            },
            headers={"x-csrf-token": csrf},
        )
        assert first.status_code == 202
        assert second.status_code == 200
        run = session.get(JobRun, "prepared-run")
        assert run is not None
        assert run.user_id == user.user_id
        assert run.status == "PENDING"
        assert run.manifest["dependency_lock_sha256"] == "d" * 64
        assert run.manifest["research_scope"] == "CUSTOM"
        assert run.manifest["target_symbols"] == ["600519.SH", "000858.SZ"]
        assert run.manifest["tracked_symbols"] == ["000858.SZ", "600519.SH"]
        assert run.manifest["research_budget"] == {
            "total_budget": "1000000",
            "per_symbol_budget": "80000",
            "max_stock_price": "500",
        }
        assert run.manifest["portfolio_requested"] is False
        assert run.input_hash != "i" * 64
        assert pipeline.start_calls == 1
        assert queued == ["prepared-run"]
        run.status = "SUCCEEDED"
        run.active_research_key = None
        session.commit()
        portfolio = client.get(
            "/api/v1/portfolios/2026-07-14",
            params={"run_id": "prepared-run"},
        )
        assert portfolio.status_code == 200
        assert portfolio.json()["research_only"] is True
        assert "少于 15 只" in portfolio.json()["message"]
    finally:
        app.dependency_overrides.clear()
        session.close()


def test_watchlist_research_accepts_a_selected_subset_and_rejects_outside_symbols(
    monkeypatch,
) -> None:
    session, _, user = _database()
    pipeline = PreparedPipeline(session)
    queued: list[str] = []
    monkeypatch.setattr("ashare_ai.api.app.load_pipeline", lambda: pipeline)
    monkeypatch.setattr("ashare_ai.api.app.enqueue_research", queued.append)

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(app)
        client.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": "alice-password"},
        )
        csrf = client.cookies.get("ashare_csrf")
        assets = client.put(
            "/api/v1/assets",
            json={"watchlist": ["600519.SH", "000858.SZ"], "positions": []},
            headers={"x-csrf-token": csrf},
        )
        assert assets.status_code == 200

        empty = client.post(
            "/api/v1/research/runs",
            json={
                "trading_date": "2026-07-14",
                "scope": "WATCHLIST",
                "symbols": [],
            },
            headers={"x-csrf-token": csrf},
        )
        assert empty.status_code == 422
        assert empty.json()["detail"] == "请至少选择一只自选股或持仓股票"

        outside = client.post(
            "/api/v1/research/runs",
            json={
                "trading_date": "2026-07-14",
                "scope": "WATCHLIST",
                "symbols": ["300750.SZ"],
            },
            headers={"x-csrf-token": csrf},
        )
        assert outside.status_code == 422
        assert "不在当前自选股或持仓中" in outside.json()["detail"]

        selected = client.post(
            "/api/v1/research/runs",
            json={
                "trading_date": "2026-07-14",
                "scope": "WATCHLIST",
                "symbols": ["600519.SH"],
                "total_budget": 1_000_000,
                "per_symbol_budget": 80_000,
            },
            headers={"x-csrf-token": csrf},
        )
        assert selected.status_code == 202
        run = session.get(JobRun, "prepared-run")
        assert run is not None
        assert run.user_id == user.user_id
        assert run.manifest["research_scope"] == "WATCHLIST"
        assert run.manifest["target_symbols"] == ["600519.SH"]
        assert run.manifest["tracked_symbols"] == ["600519.SH"]
        assert pipeline.start_calls == 1
        assert queued == ["prepared-run"]
    finally:
        app.dependency_overrides.clear()
        session.close()


def test_snapshot_listing_and_submission_hide_unusable_research_runs() -> None:
    session, _, user = _database()
    now = datetime.now(UTC)
    session.add(
        JobRun(
            run_id="owned-snapshot-run",
            user_id=user.user_id,
            run_type="DAILY",
            trading_date=date(2026, 7, 14),
            decision_at=now,
            status="SUCCEEDED",
            idempotency_key="owned-snapshot-key",
            manifest={},
            input_hash="d" * 64,
            started_at=now,
            completed_at=now,
        )
    )
    session.add(
        JobRun(
            run_id="fused-snapshot-run",
            user_id=user.user_id,
            run_type="DAILY",
            trading_date=date(2026, 7, 13),
            decision_at=now,
            status="FUSED",
            idempotency_key="fused-snapshot-key",
            manifest={},
            input_hash="9" * 64,
            started_at=now,
            completed_at=now,
        )
    )
    session.add(
        JobRun(
            run_id="failed-snapshot-run",
            user_id=user.user_id,
            run_type="DAILY",
            trading_date=date(2026, 7, 14),
            decision_at=now,
            status="FAILED",
            idempotency_key="failed-snapshot-key",
            manifest={},
            input_hash="e" * 64,
            started_at=now,
            completed_at=now,
        )
    )
    session.add_all(
        [
            SnapshotManifestRow(
                snapshot_id=f"snapshot-{count}",
                run_id="owned-snapshot-run",
                dataset="backtest_bundle",
                source="fixture",
                schema_version="1",
                adapter_version="1",
                fetched_at=now,
                row_count=10,
                payload_sha256=str(count) * 64,
                parquet_uri=f"file:///snapshot-{count}.parquet",
                status="COMMITTED",
                details={"executable_signal_count": count},
                committed_at=now,
            )
            for count in (0, 1)
        ]
    )
    session.add(
        SnapshotManifestRow(
            snapshot_id="snapshot-failed-run",
            run_id="failed-snapshot-run",
            dataset="backtest_bundle",
            source="fixture",
            schema_version="1",
            adapter_version="1",
            fetched_at=now,
            row_count=10,
            payload_sha256="f" * 64,
            parquet_uri="file:///snapshot-failed-run.parquet",
            status="COMMITTED",
            details={
                "executable_signal_count": 1,
                "calendar_start": "2026-07-01",
                "calendar_end": "2026-07-14",
                "parquet_file_sha256": "a" * 64,
            },
            committed_at=now,
        )
    )
    session.add(
        SnapshotManifestRow(
            snapshot_id="snapshot-fused-run",
            run_id="fused-snapshot-run",
            dataset="backtest_bundle",
            source="akshare",
            schema_version="1",
            adapter_version="1",
            fetched_at=now,
            row_count=10,
            payload_sha256="8" * 64,
            parquet_uri="file:///snapshot-fused-run.parquet",
            status="COMMITTED",
            details={"executable_signal_count": 1, "observe_only": True},
            committed_at=now,
        )
    )
    session.commit()

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(app)
        client.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": "alice-password"},
        )
        rows = client.get("/api/v1/snapshots?dataset=backtest_bundle").json()
        assert {item["snapshot_id"] for item in rows} == {
            "snapshot-1",
            "snapshot-fused-run",
        }
        csrf = client.cookies.get("ashare_csrf")
        rejected = client.post(
            "/api/v1/backtests",
            json={
                "name": "must-not-run",
                "start_date": "2026-07-01",
                "end_date": "2026-07-14",
                "snapshot_ids": ["snapshot-failed-run"],
                "config": {},
            },
            headers={"x-csrf-token": csrf},
        )
        assert rejected.status_code == 422
        assert (
            client.get(
                "/api/v1/snapshots?dataset=backtest_bundle&include_non_executable=true"
            ).status_code
            == 403
        )
    finally:
        app.dependency_overrides.clear()
        session.close()


def test_backtest_detail_exposes_run_and_failure_reason() -> None:
    session, _, user = _database()
    now = datetime.now(UTC)
    session.add(
        JobRun(
            run_id="failed-backtest-run",
            user_id=user.user_id,
            run_type="BACKTEST",
            trading_date=date(2026, 7, 14),
            decision_at=now,
            status="FAILED",
            idempotency_key="failed-backtest-key",
            manifest={},
            input_hash="e" * 64,
            started_at=now,
            completed_at=now,
            error_message="snapshot range unavailable",
        )
    )
    session.add(
        BacktestRun(
            backtest_id="failed-backtest",
            run_id="failed-backtest-run",
            user_id=user.user_id,
            name="failed",
            status="FAILED",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 7, 14),
            config={},
            snapshot_ids=["snapshot"],
            input_hash="e" * 64,
            created_at=now,
            completed_at=now,
        )
    )
    session.commit()

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(app)
        client.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": "alice-password"},
        )
        payload = client.get("/api/v1/backtests/failed-backtest").json()
        assert payload["run_id"] == "failed-backtest-run"
        assert payload["completed_at"] is not None
        assert payload["error_message"] == "snapshot range unavailable"
    finally:
        app.dependency_overrides.clear()
        session.close()
