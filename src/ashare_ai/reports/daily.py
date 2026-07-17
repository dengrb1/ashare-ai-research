from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape
from sqlalchemy.orm import Session

from ashare_ai.storage.models import ReportRow
from ashare_ai.storage.objects import ObjectStore


class DailyReportService:
    def __init__(self, session: Session, object_store: ObjectStore) -> None:
        self.session = session
        self.object_store = object_store
        self.environment = Environment(
            loader=PackageLoader("ashare_ai.reports", "templates"),
            autoescape=select_autoescape(["html"]),
            undefined=StrictUndefined,
        )

    def generate(
        self,
        *,
        run_id: str,
        trading_date: date,
        context: dict[str, Any],
        report_type: str = "DAILY_RESEARCH",
    ) -> ReportRow:
        content = self.environment.get_template("daily.html.j2").render(**context)
        uri, digest = self.object_store.put(content.encode("utf-8"), content_type="text/html")
        row = ReportRow(
            run_id=run_id,
            trading_date=trading_date,
            report_type=report_type,
            object_uri=uri,
            content_sha256=digest,
            created_at=datetime.now(UTC),
        )
        self.session.add(row)
        self.session.flush()
        return row
