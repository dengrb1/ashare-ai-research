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
            "report_symbols": [],
            "positions": [],
            "risks": [],
            "run_id": "run-report",
            "input_hash": "a" * 64,
            "formula_version": "v1",
            "trade_rule_version": "v1",
            "research_scope": "MARKET",
            "target_symbols": [],
            "research_budget": {},
            "research_only_reason": None,
            "portfolio_outcome": {},
            "quality_summary": {
                "symbol_count": 0,
                "fundamental_placeholder_count": 0,
                "sentiment_placeholder_count": 0,
                "industry_placeholder_count": 0,
            },
            "formal_eligible_symbols": [],
            "excluded_symbols": {},
            "risk_reason_code": None,
            "risk_reason_message": None,
        },
    )
    session.commit()
    assert store.get(report.object_uri).startswith(b"<!doctype html>")
    assert report.object_uri.endswith(report.content_sha256)


def test_daily_report_prefers_chinese_plain_language_summary(tmp_path) -> None:
    engine = create_engine("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine)
    store = LocalObjectStore(tmp_path / "objects")
    report = DailyReportService(session, store).generate(
        run_id="run-summary",
        trading_date=date(2026, 7, 14),
        context={
            "trading_date": "2026-07-14",
            "decision_at": "2026-07-14T18:00:00+08:00",
            "run_status": "SUCCEEDED",
            "fused": False,
            "candidates": [],
            "report_symbols": [
                {
                    "symbol": "600000.SH",
                    "name": "浦发银行",
                    "advice_eligible": True,
                    "recommendation": None,
                    "research_status": "FORMAL",
                    "plain_language_summary": "综合评分 70.00 分，数据门禁已通过。",
                    "exclusion_reasons": [],
                    "score": {
                        "total_score": 70,
                        "base_total_score": 70,
                        "fundamental_score": 70,
                        "technical_score": 70,
                        "sentiment_score": 70,
                        "quality_confidence_score": 70,
                        "event_risk_multiplier": 1,
                        "decision_at": "2026-07-14T18:00:00+08:00",
                    },
                    "components": {
                        "fundamental": {
                            "score": 70,
                            "confidence": 0.8,
                            "summary": "基本面得分 70.00 分，整体表现相对较好。",
                            "positive_factors": ["english factor should not be rendered"],
                            "evidence": [],
                        }
                    },
                }
            ],
            "positions": [],
            "risks": [],
            "run_id": "run-summary",
            "input_hash": "a" * 64,
            "formula_version": "v1",
            "trade_rule_version": "v1",
            "research_scope": "MARKET",
            "target_symbols": [],
            "research_budget": {},
            "research_only_reason": None,
            "portfolio_outcome": {},
            "quality_summary": {
                "symbol_count": 1,
                "fundamental_placeholder_count": 0,
                "sentiment_placeholder_count": 0,
                "industry_placeholder_count": 0,
            },
            "formal_eligible_symbols": ["600000.SH"],
            "excluded_symbols": {},
            "risk_reason_code": None,
            "risk_reason_message": None,
        },
    )
    content = store.get(report.object_uri).decode("utf-8")
    assert "给家人看的总结" in content
    assert "综合评分 70.00 分" in content
    assert "english factor should not be rendered" not in content
