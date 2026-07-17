from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import httpx
import respx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ashare_ai.api.app import app
from ashare_ai.api.auth import hash_password
from ashare_ai.api.dependencies import get_db
from ashare_ai.core.config import Settings
from ashare_ai.search.service import (
    FinancialSearchResponse,
    FinancialSearchService,
    FinancialSearchStatus,
    NeoDataFinancialSearchProvider,
    SearchEntity,
    SearchRecall,
    _minimal_subprocess_env,
    get_financial_search_service,
)
from ashare_ai.storage.models import Base, UserAccount


def test_neodata_is_default_and_blank_cli_path_is_unconfigured(monkeypatch) -> None:
    monkeypatch.setenv("NEODATA_FINANCIAL_SEARCH_PATH", "")
    settings = Settings(_env_file=None)
    assert settings.financial_search_provider == "neodata-financial-search"
    assert settings.neodata_financial_search_path is None


def test_neodata_cli_subprocess_does_not_inherit_application_secrets(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
    monkeypatch.setenv("TUSHARE_TOKEN", "secret-token")
    monkeypatch.setenv("LLM_API_KEY", "secret-key")
    child_env = _minimal_subprocess_env()
    assert "DATABASE_URL" not in child_env
    assert "TUSHARE_TOKEN" not in child_env
    assert "LLM_API_KEY" not in child_env


def test_neodata_cli_contract_is_parsed_from_query_script(tmp_path) -> None:
    payload = {
        "code": "200",
        "msg": "操作成功",
        "suc": True,
        "data": {
            "apiData": {
                "entity": [{"name": "贵州茅台", "code": "sh600519"}],
                "apiRecall": [
                    {"type": "basic_info", "desc": "行情数据", "content": "最新: 1500"}
                ],
            }
        },
    }
    script = tmp_path / "query.py"
    script.write_text(
        "import json\n"
        "print('开始查询')\n"
        "print('=== 查询结果 ===')\n"
        f"print(json.dumps({payload!r}, ensure_ascii=False))\n",
        encoding="utf-8",
    )
    provider = NeoDataFinancialSearchProvider(
        script_path=script,
        mode="auto",
        timeout_seconds=2,
    )
    result = provider.search("贵州茅台股价")
    assert result.provider == "neodata-financial-search"
    assert result.upstream == "sina-finance"
    assert result.mode == "cli"
    assert result.entities[0].code == "sh600519"
    assert result.recalls[0].content == "最新: 1500"


@respx.mock
def test_embedded_neodata_mode_supports_arbitrary_a_share_codes() -> None:
    fields = [
        "贵州茅台",
        "1490",
        "1480",
        "1500",
        "1510",
        "1470",
        "0",
        "0",
        "123456",
        "987654321",
        *(["0"] * 20),
        "2026-07-15",
        "14:58:00",
    ]
    respx.get("https://hq.sinajs.cn/list=sh600519").mock(
        return_value=httpx.Response(
            200,
            content=f'var hq_str_sh600519="{",".join(fields)}";'.encode("gbk"),
        )
    )
    service = FinancialSearchService(
        Settings(
            financial_search_provider="neodate-financial-search",
            neodata_financial_search_mode="embedded",
        )
    )
    result = service.search("查询 600519.SH")
    assert result.provider == "neodata-financial-search"
    assert result.mode == "embedded"
    assert "最新: 1500" in result.recalls[0].content
    assert result.live_data_isolated_from_snapshots is True


def test_search_cache_single_flight_and_user_rate_limit() -> None:
    service = FinancialSearchService(
        Settings(
            neodata_financial_search_mode="embedded",
            financial_search_cache_seconds=30,
            financial_search_max_concurrency=2,
            financial_search_rate_limit_per_minute=2,
        )
    )

    class FakeProvider:
        calls = 0

        def search(self, query: str) -> FinancialSearchResponse:
            self.calls += 1
            time.sleep(0.05)
            return FinancialSearchResponse(
                query=query,
                provider="neodata-financial-search",
                upstream="sina-finance",
                mode="embedded",
                searched_at=datetime.now(UTC),
                elapsed_ms=5,
                entities=(SearchEntity(name="贵州茅台", code="sh600519"),),
                recalls=(SearchRecall(type="basic_info", desc="行情", content="1500"),),
                raw_sha256="b" * 64,
            )

    provider = FakeProvider()
    service.provider = provider  # type: ignore[assignment]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(service.search, ["贵州茅台"] * 4))
    assert provider.calls == 1
    assert {item.raw_sha256 for item in results} == {"b" * 64}
    assert service.allow_user_request("user-1") is True
    assert service.allow_user_request("user-1") is True
    assert service.allow_user_request("user-1") is False


def test_authenticated_financial_search_api_uses_default_service() -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    with factory() as session:
        now = datetime.now(UTC)
        session.add(
            UserAccount(
                username="alice",
                password_hash=hash_password("alice-password"),
                role="USER",
                enabled=True,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

    class FakeSearch:
        def allow_user_request(self, user_id: str) -> bool:
            return bool(user_id)

        def search(self, query: str) -> FinancialSearchResponse:
            return FinancialSearchResponse(
                query=query,
                provider="neodata-financial-search",
                upstream="sina-finance",
                mode="embedded",
                searched_at=datetime.now(UTC),
                elapsed_ms=3,
                entities=(SearchEntity(name="贵州茅台", code="sh600519"),),
                recalls=(
                    SearchRecall(type="basic_info", desc="行情数据", content="最新: 1500"),
                ),
                raw_sha256="a" * 64,
            )

        def status(self) -> FinancialSearchStatus:
            return FinancialSearchStatus(
                provider="neodata-financial-search",
                upstream="sina-finance",
                mode="embedded",
                available=True,
                message="fixture",
            )

    def override_db():
        with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_financial_search_service] = FakeSearch
    try:
        client = TestClient(app)
        assert client.get("/api/v1/search/status").status_code == 401
        assert client.post(
            "/api/v1/auth/login",
            json={"username": "alice", "password": "alice-password"},
        ).status_code == 200
        response = client.get("/api/v1/search/financial", params={"q": "贵州茅台"})
        assert response.status_code == 200
        assert response.json()["provider"] == "neodata-financial-search"
        assert response.json()["recalls"][0]["content"] == "最新: 1500"
    finally:
        app.dependency_overrides.clear()
