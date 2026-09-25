from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ashare_ai.api.schemas import ReportResponse
from ashare_ai.storage.models import Base, JobRun, ReportRow


def test_report_contract_keeps_structured_result_and_no_object_payload() -> None:
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 14, 10, tzinfo=UTC)
    with Session(engine) as session:
        session.add(
            JobRun(
                run_id="run-report",
                run_type="DAILY",
                trading_date=date(2026, 7, 14),
                decision_at=now,
                status="SUCCEEDED",
                idempotency_key="run-report-key",
                manifest={},
                input_hash="a" * 64,
                started_at=now,
                completed_at=now,
            )
        )
        row = ReportRow(
            report_id="report-structured",
            run_id="run-report",
            trading_date=date(2026, 7, 14),
            report_type="DAILY_RESEARCH",
            object_uri=None,
            content_sha256=None,
            result={
                "trading_date": "2026-07-14",
                "report_symbols": [{"symbol": "600000.SH", "total_score": 72}],
            },
            created_at=now,
        )
        session.add(row)
        session.commit()
        payload = {column.name: getattr(row, column.name) for column in row.__table__.columns}

    response = ReportResponse.model_validate(payload)
    assert response.result["report_symbols"][0]["symbol"] == "600000.SH"
    assert response.object_uri is None
    assert response.content_sha256 is None


def test_report_response_accepts_legacy_empty_result_as_empty_structure() -> None:
    now = datetime(2026, 7, 14, 10, tzinfo=UTC)
    response = ReportResponse.model_validate(
        {
            "report_id": "legacy-report",
            "run_id": "legacy-run",
            "trading_date": date(2026, 7, 14),
            "report_type": "DAILY_RESEARCH",
            "result": {},
            "object_uri": None,
            "content_sha256": None,
            "created_at": now,
        }
    )
    assert response.result == {}
