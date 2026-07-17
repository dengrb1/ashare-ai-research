from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from importlib import import_module
from typing import Any, Protocol
from uuid import NAMESPACE_URL, uuid5

from ashare_ai.adapters.symbols import infer_exchange, normalize_symbol
from ashare_ai.core.contracts import (
    AvailabilityBasis,
    Board,
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
from ashare_ai.core.time import SHANGHAI
from ashare_ai.orchestration.bundle import CanonicalDailyBundle


class CanonicalMarketProvider(Protocol):
    source: str

    def securities(self) -> list[dict[str, Any]]: ...

    def daily_bars(self, symbol: str, start_date: date, end_date: date) -> list[dict[str, Any]]: ...

    def benchmark_bars(
        self, code: str, start_date: date, end_date: date
    ) -> list[dict[str, Any]]: ...


class AKShareCanonicalProvider:
    source = "akshare"

    @staticmethod
    def _sdk() -> Any:
        return import_module("akshare")

    def securities(self) -> list[dict[str, Any]]:
        sdk = self._sdk()
        try:
            frame = sdk.stock_zh_a_spot_em()
            return list(frame.to_dict(orient="records"))
        except Exception:
            frame = sdk.stock_zh_a_spot()
            return [
                {
                    **row,
                    "代码": _strip_market_prefix(str(row.get("代码", ""))),
                    "_canonical_source": "akshare-sina",
                }
                for row in frame.to_dict(orient="records")
            ]

    def daily_bars(
        self, symbol: str, start_date: date, end_date: date
    ) -> list[dict[str, Any]]:
        sdk = self._sdk()
        normalized = str(normalize_symbol(symbol))
        try:
            frame = sdk.stock_zh_a_hist(
                symbol=normalized.split(".", 1)[0],
                period="daily",
                start_date=start_date.strftime("%Y%m%d"),
                end_date=end_date.strftime("%Y%m%d"),
                adjust="hfq",
            )
            return list(frame.to_dict(orient="records"))
        except Exception:
            frame = sdk.stock_zh_a_daily(
                symbol=_sina_symbol(normalized),
                start_date=start_date.strftime("%Y%m%d"),
                end_date=end_date.strftime("%Y%m%d"),
                adjust="hfq",
            )
            return [
                {**row, "_canonical_source": "akshare-sina"}
                for row in frame.to_dict(orient="records")
            ]

    def benchmark_bars(
        self, code: str, start_date: date, end_date: date
    ) -> list[dict[str, Any]]:
        sdk = self._sdk()
        try:
            frame = sdk.index_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start_date.strftime("%Y%m%d"),
                end_date=end_date.strftime("%Y%m%d"),
            )
            return list(frame.to_dict(orient="records"))
        except Exception:
            frame = sdk.stock_zh_index_daily(symbol=f"sh{code}")
            rows = []
            for row in frame.to_dict(orient="records"):
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
            for item in daily.to_dict(orient="records")
        }
        return [
            {
                "代码": str(item.get("ts_code", "")).split(".", 1)[0],
                "名称": item.get("name"),
                "最新价": 1,
                "成交额": amounts.get(str(item.get("ts_code")), 0),
            }
            for item in master.to_dict(orient="records")
        ]

    def daily_bars(
        self, symbol: str, start_date: date, end_date: date
    ) -> list[dict[str, Any]]:
        frame = self.sdk.pro_bar(
            api=self.client,
            ts_code=normalize_symbol(symbol),
            start_date=start_date.strftime("%Y%m%d"),
            end_date=end_date.strftime("%Y%m%d"),
            adj="hfq",
            freq="D",
        )
        return _tushare_rows(frame)

    def benchmark_bars(
        self, code: str, start_date: date, end_date: date
    ) -> list[dict[str, Any]]:
        frame = self.client.index_daily(
            ts_code=f"{code}.SH",
            start_date=start_date.strftime("%Y%m%d"),
            end_date=end_date.strftime("%Y%m%d"),
        )
        return _tushare_rows(frame)


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

    def securities(self) -> list[dict[str, Any]]:
        try:
            rows = self.primary.securities()
            if rows:
                return self._tag(rows, self.primary.source)
        except Exception:
            pass
        return self._tag(self.fallback.securities(), self.fallback.source)

    def daily_bars(
        self, symbol: str, start_date: date, end_date: date
    ) -> list[dict[str, Any]]:
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

    def benchmark_bars(
        self, code: str, start_date: date, end_date: date
    ) -> list[dict[str, Any]]:
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

class AKShareCanonicalBundleBuilder:
    """Build a PIT-frozen bundle from real AKShare market data and labeled placeholders."""

    def __init__(
        self,
        *,
        provider: CanonicalMarketProvider | None = None,
        clock: Callable[[], datetime] | None = None,
        bundle_size: int = 20,
        history_sessions: int = 90,
    ) -> None:
        self.provider = provider or AKShareCanonicalProvider()
        self.clock = clock or (lambda: datetime.now(SHANGHAI))
        self.bundle_size = bundle_size
        self.history_sessions = history_sessions

    def build(
        self,
        trading_date: date,
        decision_at: datetime,
        *,
        required_symbols: tuple[str, ...] = (),
    ) -> CanonicalDailyBundle:
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
        if decision_at.hour < 17:
            raise RuntimeError("AKShare daily canonical data is unavailable before market close")
        market_available_at = datetime.combine(
            trading_date,
            datetime.min.time().replace(hour=17),
            tzinfo=SHANGHAI,
        )
        if fetched_at < market_available_at:
            raise RuntimeError("AKShare daily canonical data has not reached its availability time")
        if market_available_at > decision_at:
            raise RuntimeError("AKShare daily data is not available at the requested decision_at")

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
            history = self._normalized_history(
                self.provider.daily_bars(security["symbol"], start_date, trading_date),
                trading_date,
            )
            tracked_only = bool(security.get("_tracked_only"))
            has_current_history = bool(
                history and history[-1]["trading_date"] == trading_date
            )
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
            if not item[0].get("_tracked_only")
            and item[1][-1]["trading_date"] == trading_date
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
                    item["trading_date"], datetime.min.time().replace(hour=17), tzinfo=SHANGHAI
                )
                bars.append(
                    DailyBar(
                        **_pit(
                            symbol,
                            item["trading_date"],
                            available_at,
                            fetched_at,
                            f"hfq-bar-{symbol}-{item['trading_date'].isoformat()}",
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
            placeholder_at = market_available_at
            facts.extend(_neutral_facts(symbol, trading_date, placeholder_at, ingestion_id))
            disclosures.append(
                _neutral_disclosure(symbol, trading_date, placeholder_at, ingestion_id)
            )
            news.append(_neutral_news(symbol, trading_date, placeholder_at, ingestion_id))
            styles[symbol] = {
                name: 0.0
                for name in ("beta", "size", "value", "momentum", "volatility", "liquidity")
            }
            quality[symbol] = {
                "source": "+".join(history_sources),
                "market_history_real": True,
                "market_price_basis": "HFQ",
                "fundamental_placeholder": True,
                "sentiment_placeholder": True,
                "industry_placeholder": True,
                "security_master_placeholder": security["name"] == symbol,
                "list_date_placeholder": True,
                "tracked_only": tracked_only,
                "history_is_current": history_is_current,
                "completeness": 0.55,
                "official_source_ratio": 0.2,
                "evidence_coverage": 0.5,
            }

        benchmark_returns = self._benchmarks(start_date, trading_date, bars)
        return CanonicalDailyBundle(
            schema_version="canonical-daily-bundle-akshare-v2",
            trading_date=trading_date,
            decision_at=decision_at,
            next_trading_date=_next_weekday(trading_date),
            securities=tuple(securities),
            statuses=tuple(statuses),
            industries=tuple(industries),
            bars=tuple(bars),
            financial_facts=tuple(facts),
            disclosures=tuple(disclosures),
            news=tuple(news),
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
                    "_canonical_source": str(
                        item.get("_canonical_source", self.provider.source)
                    ),
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
                "_canonical_source": str(
                    item.get("_canonical_source", self.provider.source)
                ),
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
        for name, code in (
            ("CSI300", "000300"),
            ("CSI500", "000905"),
            ("CSI1000", "000852"),
        ):
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
            "tushare": "tushare-canonical-v1",
            "akshare-neutral-placeholder": "neutral-placeholder-v1",
        }.get(source, "canonical-market-fallback-v1"),
        "ingestion_run_id": ingestion_id,
        "availability_basis": basis,
    }


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
    if frame is None:
        return []
    rows = frame.to_dict(orient="records")
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
