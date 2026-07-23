from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import AwareDatetime, Field, model_validator

from ashare_ai.core.contracts import (
    AvailabilityBasis,
    Board,
    CashDividend,
    DailyBar,
    Disclosure,
    Exchange,
    FinancialFact,
    FrozenModel,
    IndustryMembership,
    NewsItem,
    SecurityMasterRecord,
    SecurityStatusRecord,
)
from ashare_ai.core.hashing import sha256_bytes
from ashare_ai.core.time import SHANGHAI, market_decision_time
from ashare_ai.portfolio.events import ActiveEventRisk, EventSeverity
from ashare_ai.portfolio.risk import DrawdownControlState


class CanonicalDailyBundle(FrozenModel):
    schema_version: str = "canonical-daily-bundle-v1"
    trading_date: date
    decision_at: AwareDatetime
    next_trading_date: date
    securities: tuple[SecurityMasterRecord, ...] = Field(min_length=15)
    # A full post-close directory is carried separately from the bounded research
    # universe so @mentions can resolve any listed security without widening the
    # expensive feature/market-history bundle.
    security_directory: tuple[SecurityMasterRecord, ...] = ()
    statuses: tuple[SecurityStatusRecord, ...] = Field(min_length=15)
    industries: tuple[IndustryMembership, ...] = Field(min_length=15)
    bars: tuple[DailyBar, ...] = Field(min_length=15)
    financial_facts: tuple[FinancialFact, ...] = Field(min_length=1)
    disclosures: tuple[Disclosure, ...] = Field(min_length=1)
    news: tuple[NewsItem, ...] = ()
    dividends: tuple[CashDividend, ...] = ()
    trading_calendar: tuple[date, ...] = ()
    calendar_source: str = "UNSPECIFIED"
    calendar_version: str = "UNSPECIFIED"
    events_by_symbol: dict[str, tuple[ActiveEventRisk, ...]] = Field(default_factory=dict)
    style_exposures: dict[str, dict[str, float]] = Field(default_factory=dict)
    nav: Decimal = Field(gt=0)
    high_watermark: Decimal = Field(gt=0)
    current_weights: dict[str, float] = Field(default_factory=dict)
    previous_risk_state: DrawdownControlState | None = None
    manual_recovery_confirmed: bool = False
    data_quality: dict[str, dict[str, Any]] = Field(default_factory=dict)
    benchmark_returns: dict[str, dict[date, float]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_bundle(self) -> CanonicalDailyBundle:
        if self.decision_at.date() != self.trading_date:
            raise ValueError("bundle decision_at must fall on trading_date")
        if self.next_trading_date <= self.trading_date:
            raise ValueError("next_trading_date must be after trading_date")
        if self.trading_calendar:
            if tuple(sorted(set(self.trading_calendar))) != self.trading_calendar:
                raise ValueError("bundle trading_calendar must be sorted and unique")
            if self.trading_date not in self.trading_calendar:
                raise ValueError("bundle trading_calendar must contain trading_date")
            following = next(
                (item for item in self.trading_calendar if item > self.trading_date), None
            )
            if following != self.next_trading_date:
                raise ValueError("next_trading_date must come from the frozen trading calendar")
        symbols = [item.symbol for item in self.securities]
        if len(symbols) != len(set(symbols)):
            raise ValueError("bundle securities must be unique by symbol")
        directory_symbols = [item.symbol for item in self.security_directory]
        if len(directory_symbols) != len(set(directory_symbols)):
            raise ValueError("bundle security_directory must be unique by symbol")
        if set(symbols) & set(directory_symbols):
            raise ValueError("bundle directory must not duplicate research securities")
        symbol_set = set(symbols)
        if {item.symbol for item in self.statuses} != symbol_set:
            raise ValueError("bundle statuses must cover every security exactly")
        if {item.symbol for item in self.industries} != symbol_set:
            raise ValueError("bundle industries must cover every security exactly")
        if not symbol_set <= {item.symbol for item in self.bars}:
            raise ValueError("bundle bars must cover every security")
        if not symbol_set <= set(self.style_exposures):
            raise ValueError("bundle style exposures must cover every security")
        if not symbol_set <= set(self.data_quality):
            raise ValueError("bundle data quality must cover every security")
        point_in_time_records = (
            *self.securities,
            *self.statuses,
            *self.industries,
            *self.bars,
            *self.financial_facts,
            *self.disclosures,
            *self.news,
            *self.dividends,
        )
        future = [
            f"{item.symbol}:{item.source_record_id}"
            for item in point_in_time_records
            if item.available_at > self.decision_at
        ]
        if future:
            raise ValueError(f"bundle contains records unavailable at decision time: {future[:5]}")
        return self


def evidence_payload(source: str, source_record_id: str) -> bytes:
    return f"{source}:{source_record_id}".encode()


def make_demo_bundle(trading_date: date) -> CanonicalDailyBundle:
    decision_at = market_decision_time(trading_date)
    next_date = _next_weekday(trading_date)
    third_future_date = _next_weekday(_next_weekday(next_date))
    ingestion_run_id = uuid5(NAMESPACE_URL, f"builtin-demo:{trading_date.isoformat()}")
    sessions = _weekdays_ending(trading_date, 65)
    securities: list[SecurityMasterRecord] = []
    statuses: list[SecurityStatusRecord] = []
    industries: list[IndustryMembership] = []
    bars: list[DailyBar] = []
    facts: list[FinancialFact] = []
    disclosures: list[Disclosure] = []
    news_items: list[NewsItem] = []
    events: dict[str, tuple[ActiveEventRisk, ...]] = {}
    styles: dict[str, dict[str, float]] = {}
    report_period = trading_date - timedelta(days=45)
    prior_period = _previous_year(report_period)

    for index in range(20):
        symbol = f"{600000 + index:06d}.SH"
        industry_code = f"I{index % 5}"
        securities.append(
            SecurityMasterRecord(
                **_pit(
                    symbol,
                    trading_date,
                    decision_at.replace(hour=9),
                    f"master-{symbol}",
                    ingestion_run_id,
                ),
                exchange=Exchange.SH,
                board=Board.MAIN,
                short_name=f"示例股份{index + 1}",
                list_date=trading_date - timedelta(days=2000 + index),
                effective_from=trading_date - timedelta(days=2000 + index),
            )
        )
        statuses.append(
            SecurityStatusRecord(
                **_pit(
                    symbol,
                    trading_date,
                    decision_at.replace(hour=9),
                    f"status-{symbol}",
                    ingestion_run_id,
                ),
                is_st=False,
                is_suspended=False,
                effective_from=trading_date - timedelta(days=30),
            )
        )
        industries.append(
            IndustryMembership(
                **_pit(
                    symbol,
                    trading_date,
                    decision_at.replace(hour=9),
                    f"industry-{symbol}",
                    ingestion_run_id,
                ),
                taxonomy="BUILTIN",
                taxonomy_version="v1",
                industry_code=industry_code,
                industry_name=f"示例行业{index % 5 + 1}",
                effective_from=trading_date - timedelta(days=365),
            )
        )
        previous_close = (Decimal("9") + Decimal(index) / Decimal("10")).quantize(Decimal("0.01"))
        for session_index, session in enumerate(sessions):
            drift = Decimal("1") + Decimal(index + 1) / Decimal("100000")
            close = (previous_close * drift).quantize(Decimal("0.01"))
            if close == previous_close:
                close += Decimal("0.01") if (session_index + index) % 3 == 0 else Decimal("0")
            opening = (previous_close * Decimal("1.001")).quantize(Decimal("0.01"))
            high = max(opening, close) + Decimal("0.03")
            low = min(opening, close) - Decimal("0.03")
            available_at = datetime(session.year, session.month, session.day, 17, tzinfo=SHANGHAI)
            record_id = f"bar-{symbol}-{session.isoformat()}"
            bars.append(
                DailyBar(
                    **_pit(
                        symbol,
                        session,
                        available_at,
                        record_id,
                        ingestion_run_id,
                    ),
                    open=opening,
                    high=high,
                    low=low,
                    close=close,
                    volume=Decimal(2_000_000 + index * 10_000),
                    amount=Decimal(100_000_000 + index * 1_000_000),
                    prev_close=previous_close,
                )
            )
            previous_close = close

        available_at = decision_at.replace(hour=15, minute=30)
        base_revenue = Decimal(1_000_000_000 + index * 10_000_000)
        base_profit = Decimal(100_000_000 + index * 1_000_000)
        current_values = {
            "REVENUE": base_revenue * Decimal("1.12"),
            "NET_PROFIT": base_profit * Decimal("1.10"),
            "TOTAL_EQUITY": Decimal(800_000_000 + index * 5_000_000),
            "TOTAL_ASSETS": Decimal(1_500_000_000 + index * 10_000_000),
            "TOTAL_LIABILITIES": Decimal(600_000_000 + index * 4_000_000),
            "OPERATING_CASH_FLOW": base_profit * Decimal("1.15"),
        }
        prior_values = {"REVENUE": base_revenue, "NET_PROFIT": base_profit}
        for period, values in ((report_period, current_values), (prior_period, prior_values)):
            for field_code, value in values.items():
                record_id = f"fact-{symbol}-{period.isoformat()}-{field_code}"
                facts.append(
                    FinancialFact(
                        **_pit(
                            symbol,
                            trading_date,
                            available_at,
                            record_id,
                            ingestion_run_id,
                        ),
                        statement_type="INCOME_OR_BALANCE",
                        report_period_end=period,
                        report_type="QUARTERLY",
                        fiscal_year=period.year,
                        fiscal_quarter=None,
                        field_code=field_code,
                        value=value,
                        unit="CNY",
                        revision_seq=0,
                    )
                )
        disclosure_id = f"disclosure-{symbol}"
        disclosures.append(
            Disclosure(
                **_pit(
                    symbol,
                    trading_date,
                    decision_at.replace(hour=16),
                    disclosure_id,
                    ingestion_run_id,
                ),
                announcement_id=disclosure_id,
                title="业绩增长并实施回购计划",
                category_codes=("PERFORMANCE", "BUYBACK"),
                published_at=decision_at.replace(hour=16),
                official_verified=True,
                official_source="BUILTIN_EXCHANGE",
                document_uri=f"builtin://{disclosure_id}",
                document_sha256=_payload_hash("builtin-demo", disclosure_id),
            )
        )
        news_id = f"news-{symbol}"
        news_items.append(
            NewsItem(
                **_pit(
                    symbol,
                    trading_date,
                    decision_at.replace(hour=16, minute=30),
                    news_id,
                    ingestion_run_id,
                ),
                news_id=news_id,
                title="公司增长趋势获得市场关注",
                published_at=decision_at.replace(hour=16, minute=30),
                publisher="BUILTIN_NEWS",
                content_sha256=_payload_hash("builtin-demo", news_id),
                related_symbols=(symbol,),
            )
        )
        events[symbol] = (
            (
                ActiveEventRisk(
                    event_id=f"event-{symbol}",
                    severity=EventSeverity.MEDIUM,
                    trusted_source=True,
                ),
            )
            if index == 1
            else ()
        )
        styles[symbol] = {
            "beta": (index % 5 - 2) / 20,
            "size": (index % 4 - 1.5) / 20,
            "value": (index % 3 - 1) / 20,
            "momentum": (index % 6 - 2.5) / 20,
            "volatility": (index % 5 - 2) / 20,
            "liquidity": (index % 4 - 1.5) / 20,
        }

    return CanonicalDailyBundle(
        trading_date=trading_date,
        decision_at=decision_at,
        next_trading_date=next_date,
        securities=tuple(securities),
        statuses=tuple(statuses),
        industries=tuple(industries),
        bars=tuple(bars),
        financial_facts=tuple(facts),
        disclosures=tuple(disclosures),
        news=tuple(news_items),
        trading_calendar=tuple(_weekdays_ending(third_future_date, 68)),
        calendar_source="builtin-demo",
        calendar_version="builtin-weekdays-v1",
        events_by_symbol=events,
        style_exposures=styles,
        nav=Decimal("10000000"),
        high_watermark=Decimal("10000000"),
        data_quality={
            symbol: {
                "source": "builtin-demo",
                "market_history_real": False,
                "fundamental_placeholder": False,
                "sentiment_placeholder": False,
                "completeness": 1.0,
                "official_source_ratio": 1.0,
                "evidence_coverage": 1.0,
            }
            for symbol in styles
        },
        benchmark_returns=_demo_benchmarks(bars),
    )


def _pit(
    symbol: str,
    trading_date: date,
    available_at: datetime,
    source_record_id: str,
    ingestion_run_id: UUID,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "trading_date": trading_date,
        "available_at": available_at,
        "source": "builtin-demo",
        "source_record_id": source_record_id,
        "fetched_at": available_at,
        "payload_sha256": _payload_hash("builtin-demo", source_record_id),
        "adapter_version": "builtin-demo-v1",
        "ingestion_run_id": ingestion_run_id,
        "availability_basis": AvailabilityBasis.DERIVED,
    }


def _payload_hash(source: str, source_record_id: str) -> str:
    return sha256_bytes(evidence_payload(source, source_record_id))


def _weekdays_ending(value: date, count: int) -> tuple[date, ...]:
    sessions: list[date] = [value]
    cursor = value - timedelta(days=1)
    while len(sessions) < count:
        if cursor.weekday() < 5:
            sessions.append(cursor)
        cursor -= timedelta(days=1)
    return tuple(reversed(sessions))


def _next_weekday(value: date) -> date:
    cursor = value + timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor += timedelta(days=1)
    return cursor


def _previous_year(value: date) -> date:
    try:
        return value.replace(year=value.year - 1)
    except ValueError:
        return value.replace(year=value.year - 1, day=28)


def _demo_benchmarks(bars: list[DailyBar]) -> dict[str, dict[date, float]]:
    by_date: dict[date, list[float]] = {}
    for bar in bars:
        if bar.prev_close:
            by_date.setdefault(bar.trading_date, []).append(float(bar.close / bar.prev_close - 1))
    equal_weight = {
        trading_date: sum(values) / len(values) for trading_date, values in sorted(by_date.items())
    }
    return {
        "CSI300": dict(equal_weight),
        "CSI500": dict(equal_weight),
        "CSI1000": dict(equal_weight),
        "EQUAL_WEIGHT_UNIVERSE": dict(equal_weight),
    }
