from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ashare_ai.api import auth as auth_module
from ashare_ai.api.app import app
from ashare_ai.api.auth import hash_password
from ashare_ai.api.dependencies import get_db
from ashare_ai.core.config import Settings
from ashare_ai.storage.models import Base, JobRun, UserAccount, UserSession


def test_native_bearer_refresh_rotation_revoke_expiry_and_disable() -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    user = UserAccount(
        username="native-user",
        password_hash=hash_password("native-password"),
        role="USER",
        enabled=True,
        created_at=now,
        updated_at=now,
    )
    session.add(user)
    session.commit()

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(app)
        anonymous = client.get("/api/v1/app/bootstrap")
        assert anonymous.status_code == 401
        assert anonymous.headers["cache-control"] == "no-store"
        assert anonymous.headers["x-content-type-options"] == "nosniff"
        issued = client.post(
            "/api/v1/auth/token",
            json={"username": "native-user", "password": "native-password"},
        )
        assert issued.status_code == 200
        first = issued.json()
        assert first["token_type"] == "bearer"
        assert first["expires_in"] == 15 * 60
        stored = session.query(UserSession).one()
        assert stored.session_type == "APP"
        assert stored.token_hash != first["access_token"]
        assert stored.refresh_token_hash != first["refresh_token"]

        headers = {"Authorization": f"Bearer {first['access_token']}"}
        assert client.get("/api/v1/auth/me", headers=headers).status_code == 200
        bootstrap = client.get("/api/v1/app/bootstrap", headers=headers)
        assert bootstrap.status_code == 200
        bootstrap_body = bootstrap.json()
        assert bootstrap_body["user"]["username"] == "native-user"
        assert bootstrap_body["assets"] == client.get(
            "/api/v1/assets", headers=headers
        ).json()
        assert bootstrap_body["capabilities"]["api_version"] == "v1"
        assert bootstrap_body["capabilities"]["authentication"] == "BEARER_REFRESH"
        assert bootstrap_body["capabilities"]["supported_research_scopes"] == [
            "MARKET",
            "WATCHLIST",
            "CUSTOM",
        ]
        assert bootstrap_body["capabilities"]["features"] == {
            "watchlist_research_selection": True,
            "formal_watchlist_reports": True,
            "report_symbol_eligibility": True,
            "trade_plan_generation": True,
            "research_cancellation": True,
            "idempotency_key": True,
            "paper_portfolio_only": True,
            "profit_exit_monitor": True,
            "stop_loss_monitor": True,
            "buy_entry_monitor": True,
            "notifications": True,
            "chat_context_metrics": True,
            "persistent_ai_chat": True,
            "chat_images_seven_day_retention": True,
            "personal_archive_export_import": True,
            "searxng_web_research": True,
        }
        assert bootstrap_body["capabilities"]["endpoints"]["report_symbols"] == (
            "/api/v1/reports/{report_id}/symbols"
        )
        assert bootstrap_body["capabilities"]["endpoints"]["exit_monitor_settings"] == (
            "/api/v1/assets/exit-monitor"
        )
        assert bootstrap_body["capabilities"]["endpoints"]["research_run"] == (
            "/api/v1/research/runs/{run_id}"
        )
        original_assets = bootstrap_body["assets"]
        monitor = client.put(
            "/api/v1/assets/exit-monitor",
            headers={**headers, "Idempotency-Key": "native-exit-monitor-valid"},
            json={"exit_monitor_enabled": True, "default_profit_trigger": "1200.50"},
        )
        assert monitor.status_code == 200
        assert monitor.json()["exit_monitor_enabled"] is True
        assert Decimal(monitor.json()["default_profit_trigger"]) == Decimal("1200.50")
        assert monitor.json()["watchlist"] == original_assets["watchlist"]
        assert monitor.json()["positions"] == original_assets["positions"]
        assert client.put(
            "/api/v1/assets/exit-monitor",
            headers={**headers, "Idempotency-Key": "native-exit-monitor-invalid"},
            json={"exit_monitor_enabled": True, "default_profit_trigger": -1},
        ).status_code == 422

        session.add(
            JobRun(
                run_id="native-research-run",
                user_id=user.user_id,
                run_type="DAILY",
                trading_date=date(2026, 7, 20),
                decision_at=now,
                status="PENDING",
                idempotency_key="native-research-key",
                manifest={},
                input_hash="a" * 64,
                started_at=now,
            )
        )
        session.commit()
        progress = client.get(
            "/api/v1/research/runs/native-research-run", headers=headers
        )
        assert progress.status_code == 200
        assert progress.json()["phase"] == "等待 Worker"
        assert progress.json()["progress"] == 0
        assert client.put(
            "/api/v1/assets",
            headers=headers,
            json={"watchlist": [], "positions": []},
        ).status_code == 200

        rotated = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}
        )
        assert rotated.status_code == 200
        second = rotated.json()
        assert second["access_token"] != first["access_token"]
        assert second["refresh_token"] != first["refresh_token"]
        assert client.get("/api/v1/auth/me", headers=headers).status_code == 401
        assert client.post(
            "/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}
        ).status_code == 401

        second_headers = {"Authorization": f"Bearer {second['access_token']}"}
        stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
        assert client.get("/api/v1/auth/me", headers=second_headers).status_code == 401
        stored.expires_at = datetime.now(UTC) + timedelta(minutes=15)
        session.commit()
        assert client.post(
            "/api/v1/auth/revoke", json={"refresh_token": second["refresh_token"]}
        ).status_code == 204
        assert client.get("/api/v1/auth/me", headers=second_headers).status_code == 401

        replacement = client.post(
            "/api/v1/auth/token",
            json={"username": "native-user", "password": "native-password"},
        ).json()
        user.enabled = False
        session.commit()
        assert client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {replacement['access_token']}"},
        ).status_code == 401
    finally:
        app.dependency_overrides.clear()
        session.close()


def test_password_endpoints_are_rate_limited_without_account_enumeration(monkeypatch) -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)

    def override_db():
        yield session

    settings = Settings(auth_login_rate_limit_per_minute=2)
    monkeypatch.setattr(auth_module, "get_settings", lambda: settings)
    with auth_module._AUTH_ATTEMPTS_LOCK:
        auth_module._AUTH_ATTEMPTS.clear()
    app.dependency_overrides[get_db] = override_db
    try:
        client = TestClient(app)
        payload = {"username": "unknown-user", "password": "wrong-password"}
        assert client.post("/api/v1/auth/login", json=payload).status_code == 401
        second = client.post("/api/v1/auth/token", json=payload)
        assert second.status_code == 401
        assert second.json()["detail"] == "invalid username or password"
        limited = client.post("/api/v1/auth/token", json=payload)
        assert limited.status_code == 429
        assert limited.headers["retry-after"]
        assert limited.json()["detail"] == "authentication rate limit exceeded"
    finally:
        with auth_module._AUTH_ATTEMPTS_LOCK:
            auth_module._AUTH_ATTEMPTS.clear()
        app.dependency_overrides.clear()
        session.close()
