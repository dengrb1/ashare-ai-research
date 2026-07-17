from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import String, create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ashare_ai.storage.models import Base, JobRun, SnapshotManifestRow, TradingRuleRow
from ashare_ai.trading.default_rules import ensure_builtin_trading_rules


def test_rule_identity_allows_same_version_and_priority_for_distinct_selectors() -> None:
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine)
    main = _rule("main", "MAIN")
    star = _rule("star", "STAR")
    session.add_all([main, star])
    session.commit()
    assert main.selector_key != star.selector_key
    assert session.query(TradingRuleRow).count() == 2


def test_rule_identity_rejects_duplicate_selector_within_version() -> None:
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add_all([_rule("first", "MAIN"), _rule("duplicate", "MAIN")])
    with pytest.raises(IntegrityError):
        session.commit()


def test_builtin_rule_strings_fit_declared_storage_lengths() -> None:
    engine = create_engine("sqlite+pysqlite://")
    TradingRuleRow.__table__.create(engine)
    with Session(engine) as session:
        ensure_builtin_trading_rules(session)
        rows = session.query(TradingRuleRow).all()

    violations = {}
    for column in TradingRuleRow.__table__.columns:
        if not isinstance(column.type, String) or column.type.length is None:
            continue
        values = [getattr(row, column.name) for row in rows]
        strings = [value for value in values if isinstance(value, str)]
        if strings and (actual := max(map(len, strings))) > column.type.length:
            violations[column.name] = (column.type.length, actual)
    assert violations == {}


def test_snapshot_identity_keeps_same_rows_across_schema_versions() -> None:
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine)
    common = {
        "dataset": "daily_bar",
        "source": "fixture",
        "adapter_version": "1",
        "fetched_at": datetime(2026, 7, 14, 18, tzinfo=UTC),
        "row_count": 1,
        "payload_sha256": "a" * 64,
        "status": "COMMITTED",
        "details": {},
    }
    session.add_all(
        [
            SnapshotManifestRow(
                snapshot_id="schema-1",
                schema_version="1",
                parquet_uri="file:///schema-1.parquet",
                **common,
            ),
            SnapshotManifestRow(
                snapshot_id="schema-2",
                schema_version="2",
                parquet_uri="file:///schema-2.parquet",
                **common,
            ),
        ]
    )
    session.commit()
    assert session.query(SnapshotManifestRow).count() == 2


def test_active_research_key_is_database_unique() -> None:
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine)
    now = datetime.now(UTC)
    common = {
        "run_type": "DAILY",
        "trading_date": date(2026, 7, 14),
        "decision_at": now,
        "status": "PENDING",
        "active_research_key": "same-user-date",
        "manifest": {},
        "input_hash": "a" * 64,
        "started_at": now,
    }
    session.add_all(
        [
            JobRun(run_id="active-1", idempotency_key="active-id-1", **common),
            JobRun(run_id="active-2", idempotency_key="active-id-2", **common),
        ]
    )
    with pytest.raises(IntegrityError):
        session.commit()


def _rule(rule_id: str, board: str) -> TradingRuleRow:
    return TradingRuleRow(
        rule_id=rule_id,
        rule_type="COMPOSITE",
        rule_version="2026-v1",
        priority=10,
        market="A",
        board=board,
        is_st=False,
        effective_from=date(2026, 1, 1),
        price_limit_ratio=Decimal("0.10") if board == "MAIN" else Decimal("0.20"),
        no_price_limit=False,
        lot_size=100,
        t_plus_one=True,
        stamp_tax_rate=Decimal("0.0005"),
        commission_rate=Decimal("0.00025"),
        minimum_commission=Decimal("5"),
        transfer_fee_rate=Decimal("0.00001"),
        details={"price_tick": "0.01"},
    )
