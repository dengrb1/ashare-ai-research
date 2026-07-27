from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from importlib import import_module
from time import sleep
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from ashare_ai.adapters._vendor import vendor_records
from ashare_ai.adapters.symbols import infer_exchange, normalize_symbol
from ashare_ai.core.contracts import (
    AvailabilityBasis,
    Board,
    CashDividend,
    DailyBar,
    Disclosure,
    Exchange,
    FinancialFact,
    IndustryMembership,
    NewsItem,
    SecurityMasterRecord,
    SecurityStatusRecord,
)
from ashare_ai.core.hashing import stable_hash
from ashare_ai.core.time import SHANGHAI, conservative_date_availability
from ashare_ai.orchestration.bundle import CanonicalDailyBundle
from ashare_ai.portfolio.events import (
    ActiveEventRisk,
    EventSeverity,
    classify_event_risks,
)


class CanonicalMarketProvider(Protocol):
    source: str

    def securities(self) -> list[dict[str, Any]]: ...

    def daily_bars(self, symbol: str, start_date: date, end_date: date) -> list[dict[str, Any]]: ...

    def benchmark_bars(
        self, code: str, start_date: date, end_date: date
    ) -> list[dict[str, Any]]: ...

    def financial_reports(self, symbol: str) -> list[dict[str, Any]]: ...

    def disclosures(
        self, symbol: str, start_date: date, end_date: date
    ) -> list[dict[str, Any]]: ...

    def news(self, symbol: str, start_date: date, end_date: date) -> list[dict[str, Any]]: ...

    def dividends(self, symbol: str) -> list[dict[str, Any]]: ...

    def industry_membership(self, symbol: str) -> list[dict[str, Any]]: ...

    def trading_calendar(self, start_date: date, end_date: date) -> list[date]: ...


class MarketDataAcquisitionError(RuntimeError):
    """Sanitized, auditable failure for a bounded canonical market-data request."""

    def __init__(
        self,
        *,
        operation: str,
        subject: str,
        attempt_count: int,
        sources: tuple[str, ...],
    ) -> None:
        self.operation = operation
        self.subject = subject
        self.attempt_count = attempt_count
        self.sources = sources
        labels = {
            "securities": "证券列表",
            "daily_bars": "股票历史行情",
            "benchmark_bars": "基准历史行情",
            "financial_reports": "三大财务报表",
            "disclosures": "巨潮公告",
            "news": "免费新闻",
            "dividends": "历史现金分红",
            "industry_membership": "个股行业信息",
            "trading_calendar": "交易日历",
        }
        label = labels.get(operation, "市场数据")
        source_text = "、".join(sources)
        super().__init__(
            f"{label}采集暂时失败（{subject}）：已受限尝试 {attempt_count} 次，"
            f"数据源为 {source_text}；请稍后重试"
        )

    def audit_details(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "subject": self.subject,
            "attempt_count": self.attempt_count,
            "sources": list(self.sources),
        }


class BenchmarkDataNotReadyError(RuntimeError):
    """Retryable failure when required benchmark returns have not caught up.

    This deliberately carries only dates and benchmark identifiers so it can be
    recorded in run audits and returned through the public, sanitized error path.
    It distinguishes a normal post-close vendor lag from an invalid research
    request: callers may reschedule the same frozen task, but must never build a
    snapshot with an incomplete return series.
    """

    reason = "BENCHMARK_DATA_NOT_READY"
    retryable = True

    def __init__(
        self,
        *,
        target_date: date,
        missing_benchmarks: tuple[str, ...],
        last_available_dates: dict[str, date | None],
        missing_dates_by_benchmark: dict[str, tuple[date, ...]] | None = None,
    ) -> None:
        self.target_date = target_date
        self.missing_benchmarks = tuple(sorted(set(missing_benchmarks)))
        self.last_available_dates = {
            name: last_available_dates.get(name) for name in self.missing_benchmarks
        }
        self.missing_dates_by_benchmark = {
            name: tuple(sorted(set(values)))
            for name, values in (missing_dates_by_benchmark or {}).items()
            if name in self.missing_benchmarks
        }
        detail_parts = []
        for name in self.missing_benchmarks:
            last_available = self.last_available_dates[name]
            last_available_text = (
                last_available.isoformat() if last_available is not None else "无"
            )
            detail_parts.append(f"{name}（最后可用日：{last_available_text}）")
        details = "；".join(detail_parts)
        super().__init__(
            f"基准数据尚未同步至目标交易日 {target_date.isoformat()}：{details}；可稍后重试"
        )

    def audit_details(self) -> dict[str, Any]:
        missing_date_summary = {
            name: {
                "count": len(values),
                "first": values[0].isoformat() if values else None,
                "last": values[-1].isoformat() if values else None,
            }
            for name, values in self.missing_dates_by_benchmark.items()
        }
        return {
            "reason": self.reason,
            "retryable": self.retryable,
            "target_date": self.target_date.isoformat(),
            "missing_benchmarks": list(self.missing_benchmarks),
            "last_available_dates": {
                name: value.isoformat() if value is not None else None
                for name, value in self.last_available_dates.items()
            },
            "missing_date_summary": missing_date_summary,
        }


class AKShareCanonicalProvider:
    source = "akshare"

    def __init__(
        self,
        *,
        max_attempts: int = 2,
        backoff_seconds: float = 1.0,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if backoff_seconds < 0:
            raise ValueError("backoff_seconds must be non-negative")
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self.sleeper = sleeper

    @staticmethod
    def _sdk() -> Any:
        return import_module("akshare")

    @staticmethod
    def _records(frame: Any) -> list[dict[str, Any]]:
        return vendor_records(frame)

    def _fetch(
        self,
        *,
        operation: str,
        subject: str,
        fetchers: tuple[tuple[str, Callable[[], list[dict[str, Any]]]], ...],
    ) -> list[dict[str, Any]]:
        attempt_count = 0
        sources = tuple(source for source, _ in fetchers)
        for round_index in range(self.max_attempts):
            for _, fetcher in fetchers:
                attempt_count += 1
                try:
                    rows = fetcher()
                except Exception:
                    rows = []
                if rows:
                    return rows
            if round_index + 1 < self.max_attempts and self.backoff_seconds:
                self.sleeper(self.backoff_seconds)
        raise MarketDataAcquisitionError(
            operation=operation,
            subject=subject,
            attempt_count=attempt_count,
            sources=sources,
        )

    def _fetch_many(
        self,
        *,
        operation: str,
        subject: str,
        fetchers: tuple[tuple[str, Callable[[], list[dict[str, Any]]]], ...],
        require_any: bool = False,
    ) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        for source, fetcher in fetchers:
            try:
                rows = fetcher()
            except Exception:
                rows = []
            collected.extend({**row, "_canonical_source": source} for row in rows)
        if require_any and not collected:
            raise MarketDataAcquisitionError(
                operation=operation,
                subject=subject,
                attempt_count=len(fetchers),
                sources=tuple(source for source, _ in fetchers),
            )
        return collected

    def securities(self) -> list[dict[str, Any]]:
        sdk = self._sdk()
        return self._fetch(
            operation="securities",
            subject="A 股证券列表",
            fetchers=(
                ("eastmoney", lambda: self._records(sdk.stock_zh_a_spot_em())),
                (
                    "sina",
                    lambda: [
                        {
                            **row,
                            "代码": _strip_market_prefix(str(row.get("代码", ""))),
                            "_canonical_source": "akshare-sina",
                        }
                        for row in self._records(sdk.stock_zh_a_spot())
                    ],
                ),
            ),
        )

    def daily_bars(self, symbol: str, start_date: date, end_date: date) -> list[dict[str, Any]]:
        sdk = self._sdk()
        normalized = str(normalize_symbol(symbol))
        return self._fetch(
            operation="daily_bars",
            subject=normalized,
            fetchers=(
                (
                    "eastmoney",
                    lambda: self._records(
                        sdk.stock_zh_a_hist(
                            symbol=normalized.split(".", 1)[0],
                            period="daily",
                            start_date=start_date.strftime("%Y%m%d"),
                            end_date=end_date.strftime("%Y%m%d"),
                            adjust="",
                        )
                    ),
                ),
                (
                    "sina",
                    lambda: [
                        {**row, "_canonical_source": "akshare-sina"}
                        for row in self._records(
                            sdk.stock_zh_a_daily(
                                symbol=_sina_symbol(normalized),
                                start_date=start_date.strftime("%Y%m%d"),
                                end_date=end_date.strftime("%Y%m%d"),
                                adjust="",
                            )
                        )
                    ],
                ),
            ),
        )

    def benchmark_bars(self, code: str, start_date: date, end_date: date) -> list[dict[str, Any]]:
        sdk = self._sdk()

        def sina_rows() -> list[dict[str, Any]]:
            frame = sdk.stock_zh_index_daily(symbol=f"sh{code}")
            rows = []
            for row in self._records(frame):
                raw_date = row.get("date")
                try:
                    value_date = date.fromisoformat(str(raw_date)[:10])
                except ValueError:
                    continue
                if start_date <= value_date <= end_date:
                    rows.append(
                        {
                            **row,
                            "amount": row.get("amount", 0),
                            "_canonical_source": "akshare-sina",
                        }
                    )
            return rows

        return self._fetch(
            operation="benchmark_bars",
            subject=code,
            fetchers=(
                (
                    "eastmoney",
                    lambda: self._records(
                        sdk.index_zh_a_hist(
                            symbol=code,
                            period="daily",
                            start_date=start_date.strftime("%Y%m%d"),
                            end_date=end_date.strftime("%Y%m%d"),
                        )
                    ),
                ),
                ("sina", sina_rows),
            ),
        )

    def financial_reports(self, symbol: str) -> list[dict[str, Any]]:
        sdk = self._sdk()
        normalized = str(normalize_symbol(symbol))
        stock = _sina_symbol(normalized)
        rows: list[dict[str, Any]] = []
        for statement in ("利润表", "资产负债表", "现金流量表"):
            def fetch_statement(statement_name: str = statement) -> list[dict[str, Any]]:
                return self._records(
                    sdk.stock_financial_report_sina(
                        stock=stock,
                        symbol=statement_name,
                    )
                )

            statement_rows = self._fetch(
                operation="financial_reports",
                subject=f"{normalized}:{statement}",
                fetchers=(("sina", fetch_statement),),
            )
            rows.extend(
                {**row, "_statement_type": statement, "_canonical_source": "akshare-sina"}
                for row in statement_rows
            )
        return rows

    def disclosures(
        self, symbol: str, start_date: date, end_date: date
    ) -> list[dict[str, Any]]:
        sdk = self._sdk()
        normalized = str(normalize_symbol(symbol))
        return self._fetch(
            operation="disclosures",
            subject=normalized,
            fetchers=(("cninfo", lambda: [
                {**row, "_canonical_source": "cninfo"}
                for row in self._records(
                    sdk.stock_zh_a_disclosure_report_cninfo(
                        symbol=normalized.split(".", 1)[0],
                        market="沪深京",
                        keyword="",
                        category="",
                        start_date=start_date.strftime("%Y%m%d"),
                        end_date=end_date.strftime("%Y%m%d"),
                    )
                )
            ]),),
        )

    def news(self, symbol: str, start_date: date, end_date: date) -> list[dict[str, Any]]:
        del start_date, end_date
        sdk = self._sdk()
        normalized = str(normalize_symbol(symbol))
        stock_code = normalized.split(".", 1)[0]
        return self._fetch_many(
            operation="news",
            subject=normalized,
            fetchers=(
                ("eastmoney", lambda: self._records(sdk.stock_news_em(symbol=stock_code))),
                ("caixin", lambda: self._records(sdk.stock_news_main_cx())),
            ),
        )

    def dividends(self, symbol: str) -> list[dict[str, Any]]:
        sdk = self._sdk()
        normalized = str(normalize_symbol(symbol))
        stock_code = normalized.split(".", 1)[0]
        return self._fetch_many(
            operation="dividends",
            subject=normalized,
            fetchers=(
                ("cninfo", lambda: self._records(sdk.stock_dividend_cninfo(symbol=stock_code))),
                (
                    "sina",
                    lambda: self._records(
                        sdk.stock_history_dividend_detail(
                            symbol=stock_code, indicator="分红", date=""
                        )
                    ),
                ),
            ),
        )

    def industry_membership(self, symbol: str) -> list[dict[str, Any]]:
        sdk = self._sdk()
        normalized = str(normalize_symbol(symbol))
        stock_code = normalized.split(".", 1)[0]

        def eastmoney_rows() -> list[dict[str, Any]]:
            rows = self._records(sdk.stock_individual_info_em(symbol=stock_code))
            industry = next(
                (
                    str(row.get("value")).strip()
                    for row in rows
                    if str(row.get("item", "")).strip() == "行业"
                    and row.get("value") is not None
                    and str(row.get("value")).strip()
                ),
                None,
            )
            if industry is None:
                return []
            return [
                {
                    "industry_name": industry,
                    "taxonomy": "EM_INDUSTRY",
                    "taxonomy_version": "em-individual-info-v1",
                    "_canonical_source": "eastmoney",
                }
            ]

        return self._fetch(
            operation="industry_membership",
            subject=normalized,
            fetchers=(("eastmoney", eastmoney_rows),),
        )

    def trading_calendar(self, start_date: date, end_date: date) -> list[date]:
        sdk = self._sdk()
        rows = self._fetch(
            operation="trading_calendar",
            subject=f"{start_date.isoformat()}:{end_date.isoformat()}",
            fetchers=(("sina", lambda: self._records(sdk.tool_trade_date_hist_sina())),),
        )
        values = {
            value
            for row in rows
            if (value := _date_value(row.get("trade_date", row.get("日期")))) is not None
            and start_date <= value <= end_date
        }
        return sorted(values)


class TushareCanonicalProvider:
    source = "tushare"

    def __init__(self, token: str) -> None:
        self.sdk = import_module("tushare")
        self.client = self.sdk.pro_api(token)

    def securities(self) -> list[dict[str, Any]]:
        master = self.client.stock_basic(
            exchange="",
            list_status="L",
            fields="ts_code,name,list_date",
        )
        daily = self.client.daily(trade_date=date.today().strftime("%Y%m%d"))
        amounts = {
            str(item.get("ts_code")): item.get("amount", 0)
            for item in vendor_records(daily)
        }
        return [
            {
                "代码": str(item.get("ts_code", "")).split(".", 1)[0],
                "名称": item.get("name"),
                "最新价": 1,
                "成交额": amounts.get(str(item.get("ts_code")), 0),
            }
            for item in vendor_records(master)
        ]

    def daily_bars(self, symbol: str, start_date: date, end_date: date) -> list[dict[str, Any]]:
        frame = self.sdk.pro_bar(
            api=self.client,
            ts_code=normalize_symbol(symbol),
            start_date=start_date.strftime("%Y%m%d"),
            end_date=end_date.strftime("%Y%m%d"),
            adj=None,
            freq="D",
        )
        return _tushare_rows(frame)

    def benchmark_bars(self, code: str, start_date: date, end_date: date) -> list[dict[str, Any]]:
        frame = self.client.index_daily(
            ts_code=f"{code}.SH",
            start_date=start_date.strftime("%Y%m%d"),
            end_date=end_date.strftime("%Y%m%d"),
        )
        return _tushare_rows(frame)

    def financial_reports(self, symbol: str) -> list[dict[str, Any]]:
        normalized = str(normalize_symbol(symbol))
        result: list[dict[str, Any]] = []
        for statement, method in (
            ("利润表", self.client.income),
            ("资产负债表", self.client.balancesheet),
            ("现金流量表", self.client.cashflow),
        ):
            frame = method(ts_code=normalized)
            if frame is None or frame.empty:
                continue
            result.extend(
                {**row, "_statement_type": statement, "_canonical_source": "tushare"}
                for row in vendor_records(frame)
            )
        return result

    def disclosures(
        self, symbol: str, start_date: date, end_date: date
    ) -> list[dict[str, Any]]:
        frame = self.client.anns_d(
            ts_code=str(normalize_symbol(symbol)),
            start_date=start_date.strftime("%Y%m%d"),
            end_date=end_date.strftime("%Y%m%d"),
        )
        if frame is None or frame.empty:
            return []
        return [
            {**row, "_canonical_source": "tushare"}
            for row in vendor_records(frame)
        ]

    def news(self, symbol: str, start_date: date, end_date: date) -> list[dict[str, Any]]:
        return []

    def dividends(self, symbol: str) -> list[dict[str, Any]]:
        frame = self.client.dividend(ts_code=str(normalize_symbol(symbol)))
        if frame is None or frame.empty:
            return []
        return [
            {**row, "_canonical_source": "tushare"}
            for row in vendor_records(frame)
        ]

    def industry_membership(self, symbol: str) -> list[dict[str, Any]]:
        frame = self.client.stock_basic(
            ts_code=str(normalize_symbol(symbol)),
            fields="ts_code,industry",
        )
        if frame is None or frame.empty:
            return []
        return [
            {
                "industry_name": industry,
                "taxonomy": "TUSHARE_INDUSTRY",
                "taxonomy_version": "tushare-stock-basic-v1",
                "_canonical_source": "tushare",
            }
            for row in vendor_records(frame)
            if (industry := str(row.get("industry") or "").strip())
        ]

    def trading_calendar(self, start_date: date, end_date: date) -> list[date]:
        frame = self.client.trade_cal(
            exchange="",
            start_date=start_date.strftime("%Y%m%d"),
            end_date=end_date.strftime("%Y%m%d"),
            is_open="1",
        )
        if frame is None or frame.empty:
            return []
        return sorted(
            value
            for row in vendor_records(frame)
            if (value := _date_value(row.get("cal_date"))) is not None
        )


class FallbackCanonicalProvider:
    source = "akshare+tushare"

    def __init__(
        self,
        primary: CanonicalMarketProvider,
        fallback: CanonicalMarketProvider,
        *,
        minimum_history_rows: int,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.minimum_history_rows = minimum_history_rows

    @staticmethod
    def _tag(rows: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
        return [{**row, "_canonical_source": source} for row in rows]

    @staticmethod
    def _tag_preserving(rows: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
        return [{"_canonical_source": source, **row} for row in rows]

    def securities(self) -> list[dict[str, Any]]:
        try:
            rows = self.primary.securities()
            if rows:
                return self._tag(rows, self.primary.source)
        except Exception:
            pass
        return self._tag(self.fallback.securities(), self.fallback.source)

    def daily_bars(self, symbol: str, start_date: date, end_date: date) -> list[dict[str, Any]]:
        try:
            rows = self.primary.daily_bars(symbol, start_date, end_date)
            if len(rows) >= self.minimum_history_rows:
                return self._tag(rows, self.primary.source)
        except Exception:
            pass
        return self._tag(
            self.fallback.daily_bars(symbol, start_date, end_date),
            self.fallback.source,
        )

    def benchmark_bars(self, code: str, start_date: date, end_date: date) -> list[dict[str, Any]]:
        try:
            rows = self.primary.benchmark_bars(code, start_date, end_date)
            if len(rows) >= self.minimum_history_rows:
                return self._tag(rows, self.primary.source)
        except Exception:
            pass
        return self._tag(
            self.fallback.benchmark_bars(code, start_date, end_date),
            self.fallback.source,
        )

    def financial_reports(self, symbol: str) -> list[dict[str, Any]]:
        try:
            rows = self.primary.financial_reports(symbol)
            if _raw_financial_rows_complete(rows):
                return rows
        except Exception:
            pass
        return self._tag(self.fallback.financial_reports(symbol), self.fallback.source)

    def disclosures(
        self, symbol: str, start_date: date, end_date: date
    ) -> list[dict[str, Any]]:
        try:
            rows = self.primary.disclosures(symbol, start_date, end_date)
            if rows:
                return rows
        except Exception:
            pass
        return self._tag(
            self.fallback.disclosures(symbol, start_date, end_date),
            self.fallback.source,
        )

    def news(self, symbol: str, start_date: date, end_date: date) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for provider in (self.primary, self.fallback):
            fetcher = getattr(provider, "news", None)
            if not callable(fetcher):
                continue
            try:
                rows.extend(
                    self._tag_preserving(fetcher(symbol, start_date, end_date), provider.source)
                )
            except Exception:
                continue
        return rows

    def dividends(self, symbol: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for provider in (self.primary, self.fallback):
            fetcher = getattr(provider, "dividends", None)
            if not callable(fetcher):
                continue
            try:
                rows.extend(self._tag(fetcher(symbol), provider.source))
            except Exception:
                continue
        return rows

    def industry_membership(self, symbol: str) -> list[dict[str, Any]]:
        for provider in (self.primary, self.fallback):
            fetcher = getattr(provider, "industry_membership", None)
            if not callable(fetcher):
                continue
            try:
                rows = fetcher(symbol)
            except Exception:
                continue
            if rows:
                return self._tag_preserving(rows, provider.source)
        return []

    def trading_calendar(self, start_date: date, end_date: date) -> list[date]:
        for provider in (self.primary, self.fallback):
            fetcher = getattr(provider, "trading_calendar", None)
            if not callable(fetcher):
                continue
            try:
                values = fetcher(start_date, end_date)
            except Exception:
                continue
            if values:
                return [value for value in values if isinstance(value, date)]
        return []


class AKShareCanonicalBundleBuilder:
    """Build a PIT-frozen bundle from free market, financial and official disclosure data."""

    def __init__(
        self,
        *,
        provider: CanonicalMarketProvider | None = None,
        clock: Callable[[], datetime] | None = None,
        bundle_size: int = 20,
        history_sessions: int = 90,
        news_window_days: int = 30,
    ) -> None:
        self.provider = provider or AKShareCanonicalProvider()
        self.clock = clock or (lambda: datetime.now(SHANGHAI))
        self.bundle_size = bundle_size
        self.history_sessions = history_sessions
        self.news_window_days = news_window_days
        self.acquisition_events: list[dict[str, Any]] = []

    def build(
        self,
        trading_date: date,
        decision_at: datetime,
        *,
        required_symbols: tuple[str, ...] = (),
    ) -> CanonicalDailyBundle:
        self.acquisition_events = []
        fetched_at = self.clock()
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=SHANGHAI)
        fetched_at = fetched_at.astimezone(SHANGHAI)
        if trading_date != fetched_at.date() and not self._is_latest_completed_session(
            trading_date,
            fetched_at,
        ):
            raise RuntimeError(
                "AKShare canonical acquisition only supports today's post-close data or "
                "the latest completed session before the next market opens; use a frozen "
                "canonical file for older historical runs"
            )
        if (decision_at.hour, decision_at.minute) < (15, 5):
            raise RuntimeError("AKShare daily canonical data is unavailable before market close")
        market_available_at = datetime.combine(
            trading_date,
            datetime.min.time().replace(hour=15, minute=5),
            tzinfo=SHANGHAI,
        )
        if fetched_at < market_available_at:
            raise RuntimeError("AKShare daily canonical data has not reached its availability time")
        if market_available_at > decision_at:
            raise RuntimeError("AKShare daily data is not available at the requested decision_at")

        calendar_fetcher = getattr(self.provider, "trading_calendar", None)
        if callable(calendar_fetcher):
            trading_calendar = tuple(
                sorted(
                    set(
                        calendar_fetcher(
                            trading_date - timedelta(days=max(400, self.history_sessions * 2)),
                            trading_date + timedelta(days=30),
                        )
                    )
                )
            )
            if trading_date in trading_calendar:
                next_trading_date = next(
                    (value for value in trading_calendar if value > trading_date), None
                )
            else:
                next_trading_date = None
            if next_trading_date is not None:
                calendar_source = f"{self.provider.source}-calendar"
                calendar_version = "exchange-calendar-v1"
            elif _requires_authoritative_calendar(self.provider):
                raise RuntimeError("authoritative trading calendar is missing required sessions")
            else:
                next_trading_date = _next_weekday(trading_date)
                trading_calendar = tuple(
                    _weekdays_between(trading_date - timedelta(days=400), next_trading_date)
                )
                calendar_source = "compatibility-weekdays"
                calendar_version = "compatibility-v1"
        else:
            next_trading_date = _next_weekday(trading_date)
            trading_calendar = tuple(
                _weekdays_between(trading_date - timedelta(days=400), next_trading_date)
            )
            calendar_source = "compatibility-weekdays"
            calendar_version = "compatibility-v1"

        security_rows = self.provider.securities()
        candidates = self._candidate_securities(security_rows)
        all_by_symbol = {
            item["symbol"]: item
            for item in self._candidate_securities(security_rows, include_ineligible=True)
        }
        by_symbol = {item["symbol"]: item for item in candidates}
        required: list[dict[str, Any]] = []
        for raw_symbol in required_symbols:
            symbol = str(normalize_symbol(raw_symbol))
            security = by_symbol.pop(symbol, None)
            if security is None:
                security = all_by_symbol.get(
                    symbol,
                    {
                        "symbol": symbol,
                        "name": symbol,
                        "amount": Decimal("0"),
                        "_canonical_source": self.provider.source,
                        "_is_st": False,
                        "_spot_suspended": True,
                    },
                )
                security = {**security, "_tracked_only": True}
            else:
                security = {**security, "_tracked_only": False}
            required.append(security)
        candidates = required + list(by_symbol.values())
        target_size = min(100, self.bundle_size + len(required))
        start_date = trading_date - timedelta(days=max(180, self.history_sessions * 2))
        selected: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        for security in candidates:
            try:
                raw_history = self.provider.daily_bars(security["symbol"], start_date, trading_date)
            except MarketDataAcquisitionError as exc:
                self.acquisition_events.append(
                    {**exc.audit_details(), "outcome": "skipped_nonessential_symbol"}
                )
                continue
            history = self._normalized_history(raw_history, trading_date)
            tracked_only = bool(security.get("_tracked_only"))
            has_current_history = bool(history and history[-1]["trading_date"] == trading_date)
            if tracked_only:
                if len(history) < 2:
                    continue
            elif len(history) < self.history_sessions or not has_current_history:
                continue
            selected.append((security, history[-self.history_sessions :]))
            if len(selected) == target_size:
                break
        active_selected = [
            item
            for item in selected
            if not item[0].get("_tracked_only") and item[1][-1]["trading_date"] == trading_date
        ]
        if len(active_selected) < 15:
            raise RuntimeError("AKShare returned fewer than 15 securities with sufficient history")

        ingestion_id = uuid5(
            NAMESPACE_URL,
            f"akshare-canonical:{trading_date.isoformat()}:{fetched_at.isoformat()}",
        )
        securities: list[SecurityMasterRecord] = []
        statuses: list[SecurityStatusRecord] = []
        industries: list[IndustryMembership] = []
        bars: list[DailyBar] = []
        facts: list[FinancialFact] = []
        disclosures: list[Disclosure] = []
        news: list[NewsItem] = []
        dividends: list[CashDividend] = []
        events: dict[str, tuple[ActiveEventRisk, ...]] = {}
        styles: dict[str, dict[str, float]] = {}
        quality: dict[str, dict[str, Any]] = {}
        for security, history in selected:
            symbol = security["symbol"]
            security_source = str(security.get("_canonical_source", self.provider.source))
            history_sources = sorted(
                {str(item.get("_canonical_source", self.provider.source)) for item in history}
            )
            tracked_only = bool(security.get("_tracked_only"))
            history_is_current = history[-1]["trading_date"] == trading_date
            exchange = infer_exchange(symbol.split(".", 1)[0])
            board = _board(symbol)
            list_date = history[0]["trading_date"] - timedelta(days=365)
            master_payload = {"security": security, "fetched_at": fetched_at.isoformat()}
            securities.append(
                SecurityMasterRecord(
                    **_pit(
                        symbol,
                        trading_date,
                        market_available_at,
                        fetched_at,
                        f"security-{symbol}",
                        ingestion_id,
                        security_source,
                        master_payload,
                        AvailabilityBasis.DATE_ONLY_CONSERVATIVE,
                    ),
                    exchange=Exchange(exchange.value),
                    board=board,
                    short_name=security["name"],
                    list_date=list_date,
                    effective_from=list_date,
                )
            )
            is_st = bool(security.get("_is_st")) or "ST" in security["name"].upper()
            is_suspended = bool(security.get("_spot_suspended")) or not history_is_current
            statuses.append(
                SecurityStatusRecord(
                    **_pit(
                        symbol,
                        trading_date,
                        market_available_at,
                        fetched_at,
                        f"status-{symbol}",
                        ingestion_id,
                        security_source,
                        {"is_st": is_st, "is_suspended": is_suspended},
                        AvailabilityBasis.DATE_ONLY_CONSERVATIVE,
                    ),
                    is_st=is_st,
                    is_suspended=is_suspended,
                    effective_from=trading_date,
                )
            )
            industry_rows: list[dict[str, Any]] = []
            industry_error: str | None = None
            industry_fetcher = getattr(self.provider, "industry_membership", None)
            try:
                if callable(industry_fetcher):
                    industry_rows = industry_fetcher(symbol)
            except Exception as exc:
                industry_error = type(exc).__name__
                self.acquisition_events.append(
                    {
                        "operation": "industry_membership",
                        "subject": symbol,
                        "outcome": "placeholder_for_symbol",
                        "error_type": industry_error,
                    }
                )
            industry_row = next(
                (row for row in industry_rows if str(row.get("industry_name") or "").strip()),
                None,
            )
            industry_placeholder = industry_row is None
            if industry_row is not None:
                industry_name = str(industry_row["industry_name"]).strip()
                # A real vendor observation, so not DERIVED; the vendor only exposes
                # a current-classification snapshot without its own timestamp, so we
                # conservatively assert post-close availability via
                # market_available_at, matching SecurityMasterRecord/StatusRecord.
                # FIRST_OBSERVED would require available_at == fetched_at and trip
                # the bundle future-record validation.
                industries.append(
                    IndustryMembership(
                        **_pit(
                            symbol,
                            trading_date,
                            market_available_at,
                            fetched_at,
                            f"industry-{symbol}",
                            ingestion_id,
                            str(industry_row.get("_canonical_source", self.provider.source)),
                            industry_row,
                            AvailabilityBasis.DATE_ONLY_CONSERVATIVE,
                        ),
                        taxonomy=str(industry_row.get("taxonomy", "EM_INDUSTRY")),
                        taxonomy_version=str(
                            industry_row.get("taxonomy_version", "em-individual-info-v1")
                        ),
                        # The vendor has no industry code; the industry name itself is
                        # the deterministic code (fits CandidateRow.industry_code).
                        industry_code=industry_name,
                        industry_name=industry_name,
                        effective_from=trading_date,
                    )
                )
            else:
                industry_bucket = int(stable_hash(symbol)[:8], 16) % 5
                industries.append(
                    IndustryMembership(
                        **_pit(
                            symbol,
                            trading_date,
                            market_available_at,
                            fetched_at,
                            f"industry-placeholder-{symbol}",
                            ingestion_id,
                            "akshare-neutral-placeholder",
                            {"reason": "AKShare spot/history has no PIT industry taxonomy"},
                            AvailabilityBasis.DERIVED,
                        ),
                        taxonomy="PLACEHOLDER",
                        taxonomy_version="neutral-v1",
                        industry_code=f"PLACEHOLDER_{industry_bucket}",
                        industry_name=f"未知行业占位桶 {industry_bucket + 1}",
                        effective_from=trading_date,
                    )
                )
            previous: Decimal | None = None
            for item in history:
                available_at = datetime.combine(
                    item["trading_date"],
                    datetime.min.time().replace(hour=15, minute=5),
                    tzinfo=SHANGHAI,
                )
                bars.append(
                    DailyBar(
                        **_pit(
                            symbol,
                            item["trading_date"],
                            available_at,
                            fetched_at,
                            f"raw-bar-{symbol}-{item['trading_date'].isoformat()}",
                            ingestion_id,
                            str(item.get("_canonical_source", self.provider.source)),
                            item,
                            AvailabilityBasis.DATE_ONLY_CONSERVATIVE,
                        ),
                        open=item["open"],
                        high=item["high"],
                        low=item["low"],
                        close=item["close"],
                        volume=item["volume"],
                        amount=item["amount"],
                        prev_close=previous,
                    )
                )
                previous = item["close"]
            financial_rows: list[dict[str, Any]] = []
            disclosure_rows: list[dict[str, Any]] = []
            news_rows: list[dict[str, Any]] = []
            dividend_rows: list[dict[str, Any]] = []
            financial_error: str | None = None
            disclosure_error: str | None = None
            financial_fetcher = getattr(self.provider, "financial_reports", None)
            try:
                if callable(financial_fetcher):
                    financial_rows = financial_fetcher(symbol)
            except Exception as exc:
                financial_error = type(exc).__name__
                self.acquisition_events.append(
                    {
                        "operation": "financial_reports",
                        "subject": symbol,
                        "outcome": "placeholder_for_symbol",
                        "error_type": financial_error,
                    }
                )
            disclosure_fetcher = getattr(self.provider, "disclosures", None)
            try:
                if callable(disclosure_fetcher):
                    disclosure_rows = disclosure_fetcher(
                        symbol, trading_date - timedelta(days=365), trading_date
                    )
            except Exception as exc:
                disclosure_error = type(exc).__name__
                self.acquisition_events.append(
                    {
                        "operation": "disclosures",
                        "subject": symbol,
                        "outcome": "placeholder_for_symbol",
                        "error_type": disclosure_error,
                    }
                )
            news_fetcher = getattr(self.provider, "news", None)
            try:
                if callable(news_fetcher):
                    news_rows = news_fetcher(
                        symbol, trading_date - timedelta(days=30), trading_date
                    )
            except Exception as exc:
                self.acquisition_events.append(
                    {
                        "operation": "news",
                        "subject": symbol,
                        "outcome": "news_unavailable_for_symbol",
                        "error_type": type(exc).__name__,
                    }
                )
            dividend_fetcher = getattr(self.provider, "dividends", None)
            try:
                if callable(dividend_fetcher):
                    dividend_rows = dividend_fetcher(symbol)
            except Exception as exc:
                self.acquisition_events.append(
                    {
                        "operation": "dividends",
                        "subject": symbol,
                        "outcome": "dividends_unavailable_for_symbol",
                        "error_type": type(exc).__name__,
                    }
                )
            symbol_facts = _financial_facts(
                symbol=symbol,
                trading_date=trading_date,
                decision_at=decision_at,
                fetched_at=fetched_at,
                ingestion_id=ingestion_id,
                rows=financial_rows,
            )
            fundamental_reasons = _fundamental_completeness_reasons(symbol_facts)
            if fundamental_reasons:
                symbol_facts.extend(
                    _neutral_facts(symbol, trading_date, market_available_at, ingestion_id)
                )
            facts.extend(symbol_facts)
            symbol_disclosures = _official_disclosures(
                symbol=symbol,
                trading_date=trading_date,
                decision_at=decision_at,
                fetched_at=fetched_at,
                ingestion_id=ingestion_id,
                rows=disclosure_rows,
            )
            sentiment_reasons: list[str] = []
            if not symbol_disclosures:
                sentiment_reasons.append("MISSING_OFFICIAL_DISCLOSURE")
                symbol_disclosures = [
                    _neutral_disclosure(symbol, trading_date, market_available_at, ingestion_id)
                ]
                news.append(_neutral_news(symbol, trading_date, market_available_at, ingestion_id))
            symbol_news = _news_items(
                symbol=symbol,
                trading_date=trading_date,
                decision_at=decision_at,
                fetched_at=fetched_at,
                ingestion_id=ingestion_id,
                rows=news_rows,
            )
            if symbol_news:
                news.extend(symbol_news)
                sentiment_reasons = [
                    reason for reason in sentiment_reasons if reason != "MISSING_FREE_NEWS"
                ]
            symbol_dividends = _cash_dividends(
                symbol=symbol,
                trading_date=trading_date,
                decision_at=decision_at,
                fetched_at=fetched_at,
                ingestion_id=ingestion_id,
                rows=dividend_rows,
            )
            dividends.extend(symbol_dividends)
            disclosures.extend(symbol_disclosures)
            classified_events = classify_event_risks(
                symbol_disclosures,
                symbol_news,
                symbol=symbol,
                decision_at=decision_at,
                window_days=self.news_window_days,
            )
            events[symbol] = tuple(
                {
                    item.event_id: item
                    for item in (*_events_from_disclosures(symbol_disclosures), *classified_events)
                }.values()
            )
            styles[symbol] = {
                name: 0.0
                for name in ("beta", "size", "value", "momentum", "volatility", "liquidity")
            }
            quality[symbol] = {
                "source": "+".join(history_sources),
                "market_history_real": True,
                "market_price_basis": "RAW",
                "fundamental_placeholder": bool(fundamental_reasons),
                "sentiment_placeholder": bool(sentiment_reasons),
                "industry_placeholder": industry_placeholder,
                "industry_source": _record_sources(industry_rows),
                "industry_acquisition_error": industry_error,
                "security_master_placeholder": security["name"] == symbol,
                "list_date_placeholder": True,
                "tracked_only": tracked_only,
                "history_is_current": history_is_current,
                "fundamental_reason_codes": fundamental_reasons,
                "sentiment_reason_codes": sentiment_reasons,
                "financial_source": _record_sources(financial_rows),
                "disclosure_source": _record_sources(disclosure_rows),
                "news_sources": _record_sources(news_rows),
                "dividend_sources": _record_sources(dividend_rows),
                "financial_acquisition_error": financial_error,
                "disclosure_acquisition_error": disclosure_error,
                "completeness": 1.0 if not fundamental_reasons and not sentiment_reasons else 0.55,
                "official_source_ratio": 1.0 if not sentiment_reasons else 0.0,
                "evidence_coverage": 1.0 if not sentiment_reasons else 0.5,
            }

        # Preserve the full post-close directory as immutable evidence. The selected
        # research universe remains intentionally bounded, while chat resolution can
        # safely bind any current listed name/code using this committed projection.
        selected_symbols = {item.symbol for item in securities}
        security_directory: list[SecurityMasterRecord] = []
        for security in sorted(all_by_symbol.values(), key=lambda item: item["symbol"]):
            symbol = security["symbol"]
            if symbol in selected_symbols:
                continue
            source = f"{security.get('_canonical_source', self.provider.source)}-directory"
            security_directory.append(
                SecurityMasterRecord(
                    **_pit(
                        symbol,
                        trading_date,
                        market_available_at,
                        fetched_at,
                        f"directory-{symbol}",
                        ingestion_id,
                        source,
                        {"security": security, "fetched_at": fetched_at.isoformat()},
                        AvailabilityBasis.DATE_ONLY_CONSERVATIVE,
                    ),
                    exchange=Exchange(infer_exchange(symbol.split(".", 1)[0]).value),
                    board=_board(symbol),
                    short_name=security["name"],
                    # A directory-only record supports identity lookup, never trading
                    # rule eligibility. Its conservative effective date prevents it
                    # from being mistaken for historical master evidence.
                    list_date=trading_date,
                    effective_from=trading_date,
                )
            )

        benchmark_returns = self._benchmarks(start_date, trading_date, bars)
        return CanonicalDailyBundle(
            schema_version="canonical-daily-bundle-akshare-v3",
            trading_date=trading_date,
            decision_at=decision_at,
            next_trading_date=next_trading_date,
            securities=tuple(securities),
            security_directory=tuple(security_directory),
            statuses=tuple(statuses),
            industries=tuple(industries),
            bars=tuple(bars),
            financial_facts=tuple(facts),
            disclosures=tuple(disclosures),
            news=tuple(news),
            dividends=tuple(dividends),
            trading_calendar=trading_calendar,
            calendar_source=calendar_source,
            calendar_version=calendar_version,
            events_by_symbol=events,
            style_exposures=styles,
            nav=Decimal("10000000"),
            high_watermark=Decimal("10000000"),
            data_quality=quality,
            benchmark_returns=benchmark_returns,
        )

    def _is_latest_completed_session(
        self,
        trading_date: date,
        fetched_at: datetime,
    ) -> bool:
        """Allow a fail-closed next-morning freeze of the just-completed session.

        AKShare's spot snapshot has no reliable exchange timestamp.  It is therefore safe
        for a prior session only while the next weekday has not reached pre-open and only
        when the benchmark history proves no later completed session exists.
        """
        if trading_date >= fetched_at.date():
            return False
        if fetched_at.weekday() < 5 and (fetched_at.hour, fetched_at.minute) >= (9, 0):
            return False
        rows = self._normalized_history(
            self.provider.benchmark_bars(
                "000300",
                trading_date - timedelta(days=10),
                fetched_at.date(),
            ),
            fetched_at.date(),
        )
        return bool(rows) and rows[-1]["trading_date"] == trading_date

    def _candidate_securities(
        self,
        rows: list[dict[str, Any]],
        *,
        include_ineligible: bool = False,
    ) -> list[dict[str, Any]]:
        result = []
        for item in rows:
            code = str(item.get("代码", item.get("code", ""))).zfill(6)
            name = str(item.get("名称", item.get("name", ""))).strip()
            try:
                symbol = str(normalize_symbol(code))
            except ValueError:
                continue
            price = _decimal(item.get("最新价", item.get("price")))
            amount = _decimal(item.get("成交额", item.get("amount")))
            if not name:
                continue
            is_st = "ST" in name.upper()
            is_delisting = "退" in name
            eligible = (
                price is not None
                and price > 0
                and amount is not None
                and amount > 0
                and not is_st
                and not is_delisting
            )
            if not include_ineligible and not eligible:
                continue
            result.append(
                {
                    "symbol": symbol,
                    "name": name,
                    "amount": amount or Decimal("0"),
                    "_canonical_source": str(item.get("_canonical_source", self.provider.source)),
                    "_is_st": is_st,
                    "_spot_suspended": is_delisting or price is None or price <= 0,
                }
            )
        return sorted(result, key=lambda item: (-item["amount"], item["symbol"]))

    def _normalized_history(
        self, rows: list[dict[str, Any]], trading_date: date
    ) -> list[dict[str, Any]]:
        normalized = []
        for item in rows:
            raw_date = item.get("日期", item.get("date", item.get("timestamp")))
            try:
                value_date = date.fromisoformat(str(raw_date)[:10].replace("/", "-"))
            except (TypeError, ValueError):
                continue
            if value_date > trading_date:
                continue
            values = {
                "trading_date": value_date,
                "open": _decimal(item.get("开盘", item.get("open"))),
                "high": _decimal(item.get("最高", item.get("high"))),
                "low": _decimal(item.get("最低", item.get("low"))),
                "close": _decimal(item.get("收盘", item.get("close"))),
                "volume": _decimal(item.get("成交量", item.get("volume"))),
                "amount": _decimal(item.get("成交额", item.get("amount"))),
                "_canonical_source": str(item.get("_canonical_source", self.provider.source)),
            }
            if any(
                values[key] is None for key in ("open", "high", "low", "close", "volume", "amount")
            ):
                continue
            normalized.append(values)
        return sorted(normalized, key=lambda item: item["trading_date"])

    def _benchmarks(
        self, start_date: date, trading_date: date, bars: list[DailyBar]
    ) -> dict[str, dict[date, float]]:
        result: dict[str, dict[date, float]] = {}
        benchmark_codes = (
            ("CSI300", "000300"),
            ("CSI500", "000905"),
            ("CSI1000", "000852"),
        )
        for name, code in benchmark_codes:
            rows = self._normalized_history(
                self.provider.benchmark_bars(code, start_date, trading_date), trading_date
            )
            result[name] = _returns(rows)
            if len(result[name]) < 40:
                raise RuntimeError(f"AKShare benchmark history is incomplete: {name}")
        by_date: dict[date, list[float]] = {}
        for bar in bars:
            if bar.prev_close:
                by_date.setdefault(bar.trading_date, []).append(
                    float(bar.close / bar.prev_close - 1)
                )
        result["EQUAL_WEIGHT_UNIVERSE"] = {
            value_date: sum(values) / len(values) for value_date, values in sorted(by_date.items())
        }
        required_returns = (*[name for name, _ in benchmark_codes], "EQUAL_WEIGHT_UNIVERSE")
        missing = tuple(
            name for name in required_returns if trading_date not in result.get(name, {})
        )
        if missing:
            raise BenchmarkDataNotReadyError(
                target_date=trading_date,
                missing_benchmarks=missing,
                last_available_dates={
                    name: max(result.get(name, {}), default=None) for name in missing
                },
                missing_dates_by_benchmark={name: (trading_date,) for name in missing},
            )
        return result


def _pit(
    symbol: str,
    trading_date: date,
    available_at: datetime,
    fetched_at: datetime,
    record_id: str,
    ingestion_id: Any,
    source: str,
    payload: Any,
    basis: AvailabilityBasis,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "trading_date": trading_date,
        "available_at": available_at,
        "source": source,
        "source_record_id": record_id,
        "fetched_at": fetched_at,
        "payload_sha256": stable_hash(payload),
        "adapter_version": {
            "akshare": "akshare-canonical-v2",
            "akshare-sina": "akshare-sina-canonical-v1",
            "cninfo": "cninfo-disclosure-v1",
            "tushare": "tushare-canonical-v1",
            "akshare-neutral-placeholder": "neutral-placeholder-v1",
        }.get(source, "canonical-market-fallback-v1"),
        "ingestion_run_id": ingestion_id,
        "availability_basis": basis,
    }


_FINANCIAL_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "REVENUE": ("营业总收入", "营业收入", "total_revenue", "revenue"),
    "NET_PROFIT": (
        "归属于母公司所有者的净利润",
        "归属于母公司股东的净利润",
        "归属母公司股东的净利润",
        "n_income_attr_p",
    ),
    "TOTAL_ASSETS": ("资产总计", "total_assets"),
    "TOTAL_LIABILITIES": ("负债合计", "total_liab"),
    "TOTAL_EQUITY": (
        "归属于母公司股东权益合计",
        "归属于母公司所有者权益合计",
        "所有者权益(或股东权益)合计",
        "total_hldr_eqy_exc_min_int",
        "total_hldr_eqy_inc_min_int",
    ),
    "OPERATING_CASH_FLOW": ("经营活动产生的现金流量净额", "n_cashflow_act"),
}


def _raw_financial_rows_complete(rows: list[dict[str, Any]]) -> bool:
    by_period: dict[date, set[str]] = {}
    for row in rows:
        period = _date_value(row.get("报告日", row.get("end_date")))
        if period is None:
            continue
        fields = by_period.setdefault(period, set())
        for field, aliases in _FINANCIAL_FIELD_ALIASES.items():
            if any(_decimal(row.get(alias)) is not None for alias in aliases):
                fields.add(field)
    if not by_period:
        return False
    latest = max(by_period)
    return set(_FINANCIAL_FIELD_ALIASES) <= by_period[latest] and {
        "REVENUE",
        "NET_PROFIT",
    } <= by_period.get(_previous_year(latest), set())


def _financial_facts(
    *,
    symbol: str,
    trading_date: date,
    decision_at: datetime,
    fetched_at: datetime,
    ingestion_id: Any,
    rows: list[dict[str, Any]],
) -> list[FinancialFact]:
    result: list[FinancialFact] = []
    for row_index, row in enumerate(rows):
        report_period = _date_value(
            row.get("报告日", row.get("end_date", row.get("report_period")))
        )
        if report_period is None or report_period > trading_date:
            continue
        announced = _datetime_value(
            row.get("公告日期", row.get("f_ann_date", row.get("ann_date"))),
            date_only=True,
        )
        updated = _datetime_value(row.get("更新日期", row.get("update_time")))
        available_at = max(value for value in (announced, updated) if value is not None) if (
            announced is not None or updated is not None
        ) else fetched_at
        if available_at > decision_at:
            continue
        statement_type = str(row.get("_statement_type", "UNKNOWN"))
        source = str(row.get("_canonical_source", "akshare-sina"))
        announcement_date = announced.date().isoformat() if announced else "unknown"
        announcement_id = (
            f"financial-{symbol}-{report_period.isoformat()}-{announcement_date}"
        )
        revision_seq = int(updated is not None and announced is not None and updated > announced)
        for field_code, aliases in _FINANCIAL_FIELD_ALIASES.items():
            value = next(
                (
                    _decimal(row.get(alias))
                    for alias in aliases
                    if _decimal(row.get(alias)) is not None
                ),
                None,
            )
            if value is None:
                continue
            record_id = f"{announcement_id}-{statement_type}-{field_code}-{row_index}"
            result.append(
                FinancialFact(
                    **_pit(
                        symbol,
                        trading_date,
                        available_at,
                        fetched_at,
                        record_id,
                        ingestion_id,
                        source,
                        row,
                        AvailabilityBasis.DATE_ONLY_CONSERVATIVE
                        if announced is not None and announced.microsecond == 999999
                        else AvailabilityBasis.VENDOR_TIMESTAMP,
                    ),
                    statement_type=statement_type,
                    report_period_end=report_period,
                    report_type=_report_type(report_period),
                    fiscal_year=report_period.year,
                    fiscal_quarter={3: 1, 6: 2, 9: 3, 12: 4}.get(report_period.month),
                    field_code=field_code,
                    value=value,
                    unit="CNY",
                    revision_seq=revision_seq,
                    announcement_id=announcement_id,
                )
            )
    return result


def _fundamental_completeness_reasons(facts: list[FinancialFact]) -> list[str]:
    if not facts:
        return ["MISSING_FINANCIAL_FACTS"]
    by_period: dict[date, set[str]] = {}
    for fact in facts:
        by_period.setdefault(fact.report_period_end, set()).add(fact.field_code)
    latest = max(by_period)
    reasons: list[str] = []
    if not set(_FINANCIAL_FIELD_ALIASES) <= by_period[latest]:
        reasons.append("INCOMPLETE_LATEST_FINANCIAL_PERIOD")
    prior = _previous_year(latest)
    if not {"REVENUE", "NET_PROFIT"} <= by_period.get(prior, set()):
        reasons.append("MISSING_YOY_COMPARABLE_PERIOD")
    return reasons


def _official_disclosures(
    *,
    symbol: str,
    trading_date: date,
    decision_at: datetime,
    fetched_at: datetime,
    ingestion_id: Any,
    rows: list[dict[str, Any]],
) -> list[Disclosure]:
    result: list[Disclosure] = []
    for row in rows:
        title = str(row.get("公告标题", row.get("title", ""))).strip()
        published_at = _datetime_value(
            row.get("公告时间", row.get("ann_date", row.get("publish_time"))),
            date_only="公告时间" not in row,
        )
        if not title or published_at is None or published_at > decision_at:
            continue
        uri = str(row.get("公告链接", row.get("url", ""))).strip()
        if not uri:
            continue
        fallback_id = stable_hash(
            {"symbol": symbol, "title": title, "published_at": published_at}
        )
        announcement_id = str(
            row.get("announcementId", row.get("ann_id", fallback_id))
        )
        source = str(row.get("_canonical_source", "cninfo"))
        record_id = f"disclosure-{source}-{announcement_id}"
        result.append(
            Disclosure(
                **_pit(
                    symbol,
                    trading_date,
                    published_at,
                    fetched_at,
                    record_id,
                    ingestion_id,
                    source,
                    row,
                    AvailabilityBasis.OFFICIAL_TIMESTAMP,
                ),
                announcement_id=announcement_id,
                title=title,
                category_codes=_disclosure_categories(title),
                published_at=published_at,
                official_verified=True,
                official_source="CNINFO" if source == "cninfo" else source.upper(),
                document_uri=uri,
                document_sha256=stable_hash(
                    {
                        "announcement_id": announcement_id,
                        "title": title,
                        "published_at": published_at,
                        "document_uri": uri,
                    }
                ),
            )
        )
    return sorted(
        result,
        key=lambda item: (item.published_at or item.available_at, item.announcement_id),
    )


def _news_items(
    *,
    symbol: str,
    trading_date: date,
    decision_at: datetime,
    fetched_at: datetime,
    ingestion_id: Any,
    rows: list[dict[str, Any]],
) -> list[NewsItem]:
    window_start = decision_at - timedelta(days=30)
    by_key: dict[tuple[str, date], NewsItem] = {}
    stock_code = symbol.split(".", 1)[0]
    for row in rows:
        title = str(
            row.get("新闻标题", row.get("title", row.get("tag", "")))
        ).strip()
        content = str(
            row.get("新闻内容", row.get("content", row.get("summary", "")))
        ).strip()
        source = str(row.get("_canonical_source", "unknown-free-news"))
        if source == "caixin" and stock_code not in f"{title} {content}":
            continue
        published_at = _datetime_value(
            row.get(
                "发布时间",
                row.get("published_at", row.get("publish_time", row.get("date"))),
            )
        )
        if not title or published_at is None:
            continue
        if not window_start <= published_at <= decision_at:
            continue
        uri = str(row.get("新闻链接", row.get("url", ""))).strip() or None
        publisher = str(
            row.get("文章来源", row.get("publisher", source))
        ).strip() or source
        content_sha256 = stable_hash(
            {
                "title": title,
                "content": content,
                "published_at": published_at,
                "publisher": publisher,
                "uri": uri,
            }
        )
        news_id = stable_hash(
            {
                "symbol": symbol,
                "normalized_title": "".join(title.casefold().split()),
                "published_date": published_at.date(),
                "content_sha256": content_sha256,
            }
        )
        item = NewsItem(
            **_pit(
                symbol,
                trading_date,
                published_at,
                fetched_at,
                f"news-{source}-{news_id}",
                ingestion_id,
                source,
                row,
                AvailabilityBasis.VENDOR_TIMESTAMP,
            ),
            news_id=news_id,
            title=title,
            published_at=published_at,
            publisher=publisher,
            body_uri=uri,
            content_sha256=content_sha256,
            related_symbols=(symbol,),
            official_verified=False,
            source_uri=uri,
        )
        key = ("".join(title.casefold().split()), published_at.date())
        by_key.setdefault(key, item)
    return sorted(by_key.values(), key=lambda item: (item.published_at, item.news_id))


def _cash_dividends(
    *,
    symbol: str,
    trading_date: date,
    decision_at: datetime,
    fetched_at: datetime,
    ingestion_id: Any,
    rows: list[dict[str, Any]],
) -> list[CashDividend]:
    by_key: dict[tuple[int, date, Decimal], CashDividend] = {}
    for row in rows:
        source = str(row.get("_canonical_source", "unknown-dividend"))
        announced = _datetime_value(
            row.get(
                "实施方案公告日期",
                row.get("公告日期", row.get("ann_date", row.get("imp_ann_date"))),
            ),
            date_only=True,
        )
        payment_date = _date_value(
            row.get(
                "派息日",
                row.get(
                    "除权除息日",
                    row.get("pay_date", row.get("ex_date", row.get("div_proc"))),
                ),
            )
        )
        raw_cash = _decimal(
            row.get(
                "派息比例",
                row.get("派息", row.get("cash_div_tax", row.get("cash_div"))),
            )
        )
        progress = str(row.get("进度", row.get("分红类型", row.get("div_proc", ""))))
        if announced is None or payment_date is None or raw_cash is None or raw_cash <= 0:
            continue
        if source == "sina" and progress and "实施" not in progress:
            continue
        if announced > decision_at or payment_date > decision_at.date():
            continue
        per_share = raw_cash if source == "tushare" else raw_cash / Decimal("10")
        if per_share <= 0:
            continue
        fiscal_year = _dividend_fiscal_year(row, announced.date())
        record_date = _date_value(row.get("股权登记日", row.get("record_date")))
        ex_dividend_date = _date_value(
            row.get("除权日", row.get("除权除息日", row.get("ex_date")))
        )
        source_uri = str(row.get("url", row.get("公告链接", ""))).strip() or None
        dividend_id = stable_hash(
            {
                "symbol": symbol,
                "fiscal_year": fiscal_year,
                "payment_date": payment_date,
                "cash_dividend_per_share": per_share,
            }
        )
        item = CashDividend(
            **_pit(
                symbol,
                trading_date,
                announced,
                fetched_at,
                f"dividend-{source}-{dividend_id}",
                ingestion_id,
                source,
                row,
                AvailabilityBasis.DATE_ONLY_CONSERVATIVE,
            ),
            dividend_id=dividend_id,
            fiscal_year=fiscal_year,
            implementation_announcement_date=announced.date(),
            record_date=record_date,
            ex_dividend_date=ex_dividend_date,
            payment_date=payment_date,
            cash_dividend_per_share=per_share,
            official_verified=source == "cninfo",
            source_uri=source_uri,
        )
        key = (fiscal_year, payment_date, per_share)
        current = by_key.get(key)
        if current is None or (item.official_verified and not current.official_verified):
            by_key[key] = item
    return sorted(by_key.values(), key=lambda item: (item.payment_date, item.dividend_id))


def _dividend_fiscal_year(row: dict[str, Any], announcement_date: date) -> int:
    raw = str(
        row.get("报告时间", row.get("end_date", row.get("fiscal_year", "")))
    )
    for token in raw.replace("年", "-").split("-"):
        if len(token) == 4 and token.isdigit():
            return int(token)
    return announcement_date.year - 1


def _date_value(value: Any) -> date | None:
    if value is None or str(value).strip() in {"", "None", "NaT", "nan"}:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = str(value).strip().replace("/", "-")
    if len(raw) == 8 and raw.isdigit():
        raw = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _weekdays_between(start_date: date, end_date: date) -> list[date]:
    values: list[date] = []
    current = start_date
    while current <= end_date:
        if current.weekday() < 5:
            values.append(current)
        current += timedelta(days=1)
    return values


def _requires_authoritative_calendar(provider: CanonicalMarketProvider) -> bool:
    if isinstance(provider, (AKShareCanonicalProvider, TushareCanonicalProvider)):
        return True
    return isinstance(provider, FallbackCanonicalProvider) and isinstance(
        provider.primary, (AKShareCanonicalProvider, TushareCanonicalProvider)
    )


def _datetime_value(value: Any, *, date_only: bool = False) -> datetime | None:
    if isinstance(value, datetime):
        return (
            value.replace(tzinfo=SHANGHAI)
            if value.tzinfo is None
            else value.astimezone(SHANGHAI)
        )
    value_date = _date_value(value)
    if value_date is None:
        return None
    raw = str(value).strip().replace("/", "-")
    if date_only or len(raw) <= 10 or (len(raw) == 8 and raw.isdigit()):
        return conservative_date_availability(value_date)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return conservative_date_availability(value_date)
    return parsed.replace(tzinfo=SHANGHAI) if parsed.tzinfo is None else parsed.astimezone(SHANGHAI)


def _report_type(period: date) -> str:
    return {3: "QUARTERLY", 6: "SEMIANNUAL", 9: "QUARTERLY", 12: "ANNUAL"}.get(
        period.month, "OTHER"
    )


def _previous_year(value: date) -> date:
    try:
        return value.replace(year=value.year - 1)
    except ValueError:
        return value.replace(year=value.year - 1, day=28)


def _disclosure_categories(title: str) -> tuple[str, ...]:
    mapping = {
        "业绩": "PERFORMANCE",
        "回购": "BUYBACK",
        "增持": "HOLDING_INCREASE",
        "减持": "HOLDING_DECREASE",
        "风险": "RISK",
        "处罚": "PENALTY",
        "诉讼": "LITIGATION",
        "退市": "DELISTING",
    }
    values = tuple(code for keyword, code in mapping.items() if keyword in title)
    return values or ("GENERAL",)


def _events_from_disclosures(
    disclosures: list[Disclosure],
) -> tuple[ActiveEventRisk, ...]:
    severity_by_code = {
        "RISK": EventSeverity.HIGH,
        "PENALTY": EventSeverity.HIGH,
        "LITIGATION": EventSeverity.HIGH,
        "DELISTING": EventSeverity.CRITICAL,
        "HOLDING_DECREASE": EventSeverity.MEDIUM,
    }
    result = []
    for disclosure in disclosures:
        severity = max(
            (
                severity_by_code[code]
                for code in disclosure.category_codes
                if code in severity_by_code
            ),
            default=None,
            key=lambda value: list(EventSeverity).index(value),
        )
        if severity is not None:
            result.append(
                ActiveEventRisk(
                    event_id=f"disclosure:{disclosure.announcement_id}",
                    severity=severity,
                    trusted_source=disclosure.official_verified,
                )
            )
    return tuple(result)


def _record_sources(rows: list[dict[str, Any]]) -> list[str]:
    return sorted({str(row.get("_canonical_source", "unknown")) for row in rows})


def _neutral_facts(
    symbol: str, trading_date: date, available_at: datetime, ingestion_id: Any
) -> list[FinancialFact]:
    current = trading_date - timedelta(days=45)
    try:
        prior = current.replace(year=current.year - 1)
    except ValueError:
        prior = current.replace(year=current.year - 1, day=28)
    values = {
        "REVENUE": Decimal("100"),
        "NET_PROFIT": Decimal("0"),
        "TOTAL_EQUITY": Decimal("100"),
        "TOTAL_ASSETS": Decimal("100"),
        "TOTAL_LIABILITIES": Decimal("25"),
        "OPERATING_CASH_FLOW": Decimal("0"),
    }
    result = []
    for period, fields in (
        (current, values),
        (prior, {"REVENUE": Decimal("100"), "NET_PROFIT": Decimal("0")}),
    ):
        for field, value in fields.items():
            record_id = f"neutral-fact-{symbol}-{period}-{field}"
            result.append(
                FinancialFact(
                    **_pit(
                        symbol,
                        trading_date,
                        available_at,
                        available_at,
                        record_id,
                        ingestion_id,
                        "akshare-neutral-placeholder",
                        {"field": field, "value": str(value), "placeholder": True},
                        AvailabilityBasis.DERIVED,
                    ),
                    statement_type="NEUTRAL_PLACEHOLDER",
                    report_period_end=period,
                    report_type="PLACEHOLDER",
                    fiscal_year=period.year,
                    field_code=field,
                    value=value,
                    unit="PLACEHOLDER",
                    revision_seq=0,
                )
            )
    return result


def _neutral_disclosure(
    symbol: str, trading_date: date, available_at: datetime, ingestion_id: Any
) -> Disclosure:
    record_id = f"neutral-disclosure-{symbol}-{trading_date}"
    digest = stable_hash({"record_id": record_id, "placeholder": True})
    return Disclosure(
        **_pit(
            symbol,
            trading_date,
            available_at,
            available_at,
            record_id,
            ingestion_id,
            "akshare-neutral-placeholder",
            {"record_id": record_id},
            AvailabilityBasis.DERIVED,
        ),
        announcement_id=record_id,
        title="中性占位：AKShare 未提供可验证的 PIT 公告数据",
        category_codes=("PLACEHOLDER",),
        published_at=available_at,
        official_verified=False,
        document_uri=f"placeholder://{record_id}",
        document_sha256=digest,
    )


def _neutral_news(
    symbol: str, trading_date: date, available_at: datetime, ingestion_id: Any
) -> NewsItem:
    record_id = f"neutral-news-{symbol}-{trading_date}"
    return NewsItem(
        **_pit(
            symbol,
            trading_date,
            available_at,
            available_at,
            record_id,
            ingestion_id,
            "akshare-neutral-placeholder",
            {"record_id": record_id},
            AvailabilityBasis.DERIVED,
        ),
        news_id=record_id,
        title="中性占位：AKShare 未提供可验证的 PIT 新闻数据",
        published_at=available_at,
        publisher="NEUTRAL_PLACEHOLDER",
        content_sha256=stable_hash({"record_id": record_id, "placeholder": True}),
        related_symbols=(symbol,),
    )


def _returns(rows: list[dict[str, Any]]) -> dict[date, float]:
    result: dict[date, float] = {}
    previous: Decimal | None = None
    for item in rows:
        close = item["close"]
        if previous:
            result[item["trading_date"]] = float(close / previous - 1)
        previous = close
    return result


def _tushare_rows(frame: Any) -> list[dict[str, Any]]:
    rows = vendor_records(frame)
    result: list[dict[str, Any]] = []
    for item in rows:
        raw_date = str(item.get("trade_date", ""))
        if len(raw_date) != 8 or not raw_date.isdigit():
            continue
        volume = _decimal(item.get("vol"))
        amount = _decimal(item.get("amount"))
        result.append(
            {
                "date": f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}",
                "open": item.get("open"),
                "high": item.get("high"),
                "low": item.get("low"),
                "close": item.get("close"),
                # Tushare daily volume is reported in lots and amount in CNY thousands.
                "volume": volume * 100 if volume is not None else None,
                "amount": amount * 1000 if amount is not None else None,
            }
        )
    return result


def _decimal(value: Any) -> Decimal | None:
    if value is None or value in {"", "-", "--"}:
        return None
    try:
        result = Decimal(str(value))
    except Exception:
        return None
    return result if result.is_finite() else None


def _board(symbol: str) -> Board:
    code, exchange = symbol.split(".", 1)
    if exchange == "BJ":
        return Board.BSE
    if code.startswith("688"):
        return Board.STAR
    if code.startswith(("300", "301")):
        return Board.CHINEXT
    return Board.MAIN


def _strip_market_prefix(symbol: str) -> str:
    normalized = symbol.strip().lower()
    if normalized.startswith(("sh", "sz", "bj")):
        return normalized[2:]
    return normalized


def _sina_symbol(symbol: str) -> str:
    code, exchange = symbol.split(".", 1)
    return f"{exchange.casefold()}{code}"


def _next_weekday(value: date) -> date:
    cursor = value + timedelta(days=1)
    while cursor.weekday() >= 5:
        cursor += timedelta(days=1)
    return cursor
