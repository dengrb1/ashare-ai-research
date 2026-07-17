from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ashare_ai.api.app import app
from ashare_ai.api.auth import AuthContext, hash_password
from ashare_ai.api.dependencies import get_auth_context, get_db, get_write_context
from ashare_ai.storage.models import Base, UserAccount, UserAssetState

ROOT = Path(__file__).resolve().parents[2]


def test_user_can_persist_editable_watchlist_and_simulated_positions() -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    user = UserAccount(
        user_id="asset-user",
        username="asset-user",
        password_hash="unused",
        role="USER",
        enabled=True,
        session_version=1,
        created_at=now,
        updated_at=now,
    )
    session.add(user)
    session.commit()
    context = AuthContext(user=user, session=None)  # type: ignore[arg-type]

    def override_db() -> Iterator[Session]:
        yield session

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_auth_context] = lambda: context
    app.dependency_overrides[get_write_context] = lambda: context
    try:
        client = TestClient(app)
        initial = client.get("/api/v1/assets")
        assert initial.status_code == 200
        assert "600519.SH" in initial.json()["watchlist"]

        payload: dict[str, Any] = {
            "watchlist": ["600000.sh", "000001.SZ"],
            "positions": [
                {
                    "symbol": "600000.sh",
                    "name": "浦发银行",
                    "quantity": 300,
                    "cost": 10.25,
                    "target_weight": 0.2,
                }
            ],
        }
        updated = client.put("/api/v1/assets", json=payload)
        assert updated.status_code == 200
        assert updated.json()["watchlist"] == ["600000.SH", "000001.SZ"]
        assert updated.json()["positions"][0]["quantity"] == 300

        persisted = session.get(UserAssetState, "asset-user")
        assert persisted is not None
        assert persisted.positions[0]["symbol"] == "600000.SH"
        reloaded = client.get("/api/v1/assets").json()
        assert reloaded["watchlist"] == updated.json()["watchlist"]
        assert reloaded["positions"] == updated.json()["positions"]

        invalid = client.put(
            "/api/v1/assets",
            json={
                "watchlist": [],
                "positions": [
                    {**payload["positions"][0], "target_weight": 0.7},
                    {
                        "symbol": "000001.SZ",
                        "name": "平安银行",
                        "quantity": 100,
                        "cost": 12,
                        "target_weight": 0.4,
                    },
                ],
            },
        )
        assert invalid.status_code == 422
        invalid_payloads = [
            {"watchlist": ["600000.SH", "600000.SH"], "positions": []},
            {"watchlist": ["not-a-symbol"], "positions": []},
            {
                "watchlist": [],
                "positions": [
                    payload["positions"][0],
                    {**payload["positions"][0], "name": "重复证券"},
                ],
            },
            {
                "watchlist": [],
                "positions": [{**payload["positions"][0], "quantity": 0}],
            },
            {
                "watchlist": [],
                "positions": [{**payload["positions"][0], "cost": 0}],
            },
            {
                "watchlist": [f"{index:06d}.SH" for index in range(101)],
                "positions": [],
            },
            {
                "watchlist": [],
                "positions": [
                    {
                        "symbol": f"{600000 + index:06d}.SH",
                        "name": str(index),
                        "quantity": 1,
                        "cost": 1,
                        "target_weight": 0,
                    }
                    for index in range(16)
                ],
            },
        ]
        for invalid_payload in invalid_payloads:
            assert client.put("/api/v1/assets", json=invalid_payload).status_code == 422
    finally:
        app.dependency_overrides.clear()
        session.close()


def test_assets_require_csrf_survive_relogin_and_are_isolated_by_user() -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    now = datetime.now(UTC)
    session.add_all(
        [
            UserAccount(
                user_id="alice-assets",
                username="alice-assets",
                password_hash=hash_password("alice-password"),
                role="USER",
                enabled=True,
                session_version=1,
                created_at=now,
                updated_at=now,
            ),
            UserAccount(
                user_id="bob-assets",
                username="bob-assets",
                password_hash=hash_password("bob-password"),
                role="USER",
                enabled=True,
                session_version=1,
                created_at=now,
                updated_at=now,
            ),
        ]
    )
    session.commit()

    def override_db() -> Iterator[Session]:
        yield session

    app.dependency_overrides[get_db] = override_db
    try:
        alice = TestClient(app)
        assert (
            alice.post(
                "/api/v1/auth/login",
                json={"username": "alice-assets", "password": "alice-password"},
            ).status_code
            == 200
        )
        payload = {
            "watchlist": ["600000.SH"],
            "positions": [
                {
                    "symbol": "600000.SH",
                    "name": "浦发银行",
                    "quantity": 300,
                    "cost": 10.25,
                    "target_weight": 0.2,
                }
            ],
        }
        assert alice.put("/api/v1/assets", json=payload).status_code == 403
        csrf = alice.cookies.get("ashare_csrf")
        saved = alice.put(
            "/api/v1/assets",
            json=payload,
            headers={"x-csrf-token": csrf},
        )
        assert saved.status_code == 200

        alice.cookies.clear()
        assert alice.get("/api/v1/assets").status_code == 401
        assert (
            alice.post(
                "/api/v1/auth/login",
                json={"username": "alice-assets", "password": "alice-password"},
            ).status_code
            == 200
        )
        assert alice.get("/api/v1/assets").json()["watchlist"] == ["600000.SH"]

        bob = TestClient(app)
        assert (
            bob.post(
                "/api/v1/auth/login",
                json={"username": "bob-assets", "password": "bob-password"},
            ).status_code
            == 200
        )
        assert bob.get("/api/v1/assets").json()["watchlist"] != ["600000.SH"]
        bob_csrf = bob.cookies.get("ashare_csrf")
        assert (
            bob.put(
                "/api/v1/assets",
                json={"watchlist": ["000001.SZ"], "positions": []},
                headers={"x-csrf-token": bob_csrf},
            ).status_code
            == 200
        )
        assert alice.get("/api/v1/assets").json()["watchlist"] == ["600000.SH"]
    finally:
        app.dependency_overrides.clear()
        session.close()


def test_user_asset_state_migration_creates_and_drops_expected_table() -> None:
    migration_path = ROOT / "migrations" / "versions" / "0007_user_asset_state.py"
    spec = spec_from_file_location("migration_0007_user_asset_state", migration_path)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = create_engine("sqlite+pysqlite://")
    metadata = sa.MetaData()
    sa.Table(
        "user_accounts",
        metadata,
        sa.Column("user_id", sa.String(36), primary_key=True),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        inspector = inspect(connection)
        assert "user_asset_states" in inspector.get_table_names()
        assert {column["name"] for column in inspector.get_columns("user_asset_states")} == {
            "user_id",
            "watchlist",
            "positions",
            "updated_at",
        }
        foreign_keys = inspector.get_foreign_keys("user_asset_states")
        assert foreign_keys[0]["referred_table"] == "user_accounts"
        assert foreign_keys[0]["options"].get("ondelete") == "CASCADE"
        migration.downgrade()
        assert "user_asset_states" not in inspect(connection).get_table_names()
