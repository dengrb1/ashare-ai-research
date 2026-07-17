from __future__ import annotations

from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ashare_ai.reports.daily import DailyReportService
from ashare_ai.storage.models import Base
from ashare_ai.storage.objects import LocalObjectStore


def test_daily_report_is_content_addressed(tmp_path) -> None:
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine)
    store = LocalObjectStore(tmp_path / "objects")
    report = DailyReportService(session, store).generate(
        run_id="run-report",
        trading_date=date(2026, 7, 14),
        context={
            "trading_date": "2026-07-14",
            "decision_at": "2026-07-14T18:00:00+08:00",
            "run_status": "SUCCEEDED",
            "fused": False,
            "candidates": [],
            "positions": [],
            "risks": [],
            "run_id": "run-report",
            "input_hash": "a" * 64,
            "formula_version": "v1",
            "trade_rule_version": "v1",
        },
    )
    session.commit()
    assert store.get(report.object_uri).startswith(b"<!doctype html>")
    assert report.object_uri.endswith(report.content_sha256)
