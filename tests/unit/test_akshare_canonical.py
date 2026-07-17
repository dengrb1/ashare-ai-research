from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal

from ashare_ai.core.config import Settings
from ashare_ai.core.time import SHANGHAI
from ashare_ai.orchestration.akshare_bundle import (
    AKShareCanonicalBundleBuilder,
    AKShareCanonicalProvider,
    FallbackCanonicalProvider,
    MarketDataAcquisitionError,
)
from ashare_ai.orchestration.builtin import BuiltinDailyBackend


class Provider:
    source = "akshare"

    def __init__(self, trading_date: date) -> None:
        self.trading_date = trading_date
        self.security_calls = 0
        self.sessions = _weekdays_ending(trading_date, 70)

    def securities(self):
        self.security_calls += 1
        return [
            {
                "代码": f"{600000 + index:06d}",
                "名称": f"真实行情样本{index}",
                "最新价": 10 + index / 10,
                "成交额": 1_000_000_000 - index,
            }
            for index in range(20)
        ]

    def daily_bars(self, symbol, start_date, end_date):
        del symbol, start_date, end_date
        previous = Decimal("10")
        rows = []
        for session in self.sessions:
            close = previous + Decimal("0.01")
            rows.append(
                {
                    "日期": session.isoformat(),
                    "开盘": previous,
                    "最高": close + Decimal("0.02"),
                    "最低": previous - Decimal("0.02"),
                    "收盘": close,
                    "成交量": 2_000_000,
                    "成交额": 100_000_000,
                }
            )
            previous = close
        return rows

    def benchmark_bars(self, code, start_date, end_date):
        return self.daily_bars(code, start_date, end_date)


def test_akshare_bundle_uses_real_market_history_and_labeled_neutral_placeholders() -> None:
    trading_date = date(2026, 7, 15)
    provider = Provider(trading_date)
    builder = AKShareCanonicalBundleBuilder(
        provider=provider,
        clock=lambda: datetime(2026, 7, 15, 20, tzinfo=SHANGHAI),
        bundle_size=20,
        history_sessions=65,
    )
    decision_at = datetime(2026, 7, 15, 18, tzinfo=SHANGHAI)
    bundle = builder.build(trading_date, decision_at)
    assert len(bundle.securities) == 20
    assert len(bundle.bars) == 20 * 65
    assert all(item.available_at <= decision_at for item in bundle.bars)
    assert all(item.fetched_at > decision_at for item in bundle.securities)
    assert all(item.source == "akshare-neutral-placeholder" for item in bundle.financial_facts)
    assert all(item["market_history_real"] for item in bundle.data_quality.values())
    assert all(item["fundamental_placeholder"] for item in bundle.data_quality.values())
    assert len({item.industry_code for item in bundle.industries}) == 5
    assert set(bundle.benchmark_returns) == {
        "CSI300",
        "CSI500",
        "CSI1000",
        "EQUAL_WEIGHT_UNIVERSE",
    }


def test_akshare_bundle_rejects_historical_live_reconstruction() -> None:
    provider = Provider(date(2026, 7, 14))
    builder = AKShareCanonicalBundleBuilder(
        provider=provider,
        clock=lambda: datetime(2026, 7, 15, 20, tzinfo=SHANGHAI),
        history_sessions=65,
    )
    try:
        builder.build(
            date(2026, 7, 14),
            datetime(2026, 7, 14, 18, tzinfo=SHANGHAI),
        )
    except RuntimeError as exc:
        assert "historical runs" in str(exc)
    else:
        raise AssertionError("historical live reconstruction must fail closed")


def test_akshare_bundle_can_freeze_latest_completed_session_before_next_open() -> None:
    trading_date = date(2026, 7, 14)
    provider = Provider(trading_date)
    builder = AKShareCanonicalBundleBuilder(
        provider=provider,
        clock=lambda: datetime(2026, 7, 15, 8, 30, tzinfo=SHANGHAI),
        bundle_size=20,
        history_sessions=65,
    )

    bundle = builder.build(
        trading_date,
        datetime(2026, 7, 14, 18, tzinfo=SHANGHAI),
    )

    assert bundle.trading_date == trading_date
    assert bundle.schema_version == "canonical-daily-bundle-akshare-v2"
    assert min(item.fetched_at for item in bundle.securities) == datetime(
        2026, 7, 15, 8, 30, tzinfo=SHANGHAI
    )


def test_akshare_provider_uses_sina_fallbacks_when_eastmoney_disconnects(monkeypatch) -> None:
    class Frame:
        def __init__(self, rows):
            self.rows = rows

        def to_dict(self, *, orient):
            assert orient == "records"
            return self.rows

    class SDK:
        @staticmethod
        def stock_zh_a_spot_em():
            raise ConnectionError("disconnected")

        @staticmethod
        def stock_zh_a_spot():
            return Frame([{"代码": "sh600519", "名称": "贵州茅台", "最新价": 100}])

        @staticmethod
        def stock_zh_a_hist(**kwargs):
            del kwargs
            raise ConnectionError("disconnected")

        @staticmethod
        def stock_zh_a_daily(**kwargs):
            assert kwargs["symbol"] == "sh600519"
            return Frame([{"date": date(2026, 7, 16), "close": 100}])

        @staticmethod
        def index_zh_a_hist(**kwargs):
            del kwargs
            raise ConnectionError("disconnected")

        @staticmethod
        def stock_zh_index_daily(**kwargs):
            assert kwargs["symbol"] == "sh000300"
            return Frame(
                [
                    {"date": date(2026, 7, 15), "close": 99},
                    {"date": date(2026, 7, 16), "close": 100},
                    {"date": date(2026, 7, 17), "close": 101},
                ]
            )

    monkeypatch.setattr(AKShareCanonicalProvider, "_sdk", staticmethod(lambda: SDK()))
    provider = AKShareCanonicalProvider()

    assert provider.securities()[0]["代码"] == "600519"
    assert (
        provider.daily_bars("600519.SH", date(2026, 7, 1), date(2026, 7, 16))[0][
            "_canonical_source"
        ]
        == "akshare-sina"
    )
    benchmarks = provider.benchmark_bars("000300", date(2026, 7, 16), date(2026, 7, 16))
    assert [item["date"] for item in benchmarks] == [date(2026, 7, 16)]
    assert benchmarks[0]["amount"] == 0


def test_akshare_provider_uses_sina_after_eastmoney_json_decode_failure(monkeypatch) -> None:
    class Frame:
        def to_dict(self, *, orient):
            assert orient == "records"
            return [{"代码": "sh600519", "名称": "贵州茅台", "最新价": 100}]

    class SDK:
        @staticmethod
        def stock_zh_a_spot_em():
            raise json.JSONDecodeError("No value to decode", "", 0)

        @staticmethod
        def stock_zh_a_spot():
            return Frame()

    monkeypatch.setattr(AKShareCanonicalProvider, "_sdk", staticmethod(lambda: SDK()))
    rows = AKShareCanonicalProvider(backoff_seconds=0).securities()
    assert rows[0]["代码"] == "600519"
    assert rows[0]["_canonical_source"] == "akshare-sina"


def test_akshare_provider_retries_both_sources_in_a_second_round(monkeypatch) -> None:
    calls: list[str] = []

    class Frame:
        def __init__(self, rows):
            self.rows = rows

        def to_dict(self, *, orient):
            assert orient == "records"
            return self.rows

    class SDK:
        @staticmethod
        def stock_zh_a_spot_em():
            calls.append("eastmoney")
            return Frame([])

        @staticmethod
        def stock_zh_a_spot():
            calls.append("sina")
            if calls.count("sina") == 1:
                raise TimeoutError("temporary timeout with sensitive endpoint")
            return Frame([{"代码": "sh600519", "名称": "贵州茅台", "最新价": 100}])

    monkeypatch.setattr(AKShareCanonicalProvider, "_sdk", staticmethod(lambda: SDK()))
    rows = AKShareCanonicalProvider(max_attempts=2, backoff_seconds=0).securities()
    assert rows[0]["代码"] == "600519"
    assert calls == ["eastmoney", "sina", "eastmoney", "sina"]


def test_bundle_skips_one_nonessential_stock_failure_and_still_builds() -> None:
    trading_date = date(2026, 7, 15)

    class PartiallyUnavailableProvider(Provider):
        def securities(self):
            return [
                *super().securities(),
                {"代码": "600020", "名称": "备用样本", "最新价": 12, "成交额": 1},
            ]

        def daily_bars(self, symbol, start_date, end_date):
            if symbol == "600000.SH":
                raise MarketDataAcquisitionError(
                    operation="daily_bars",
                    subject=symbol,
                    attempt_count=4,
                    sources=("eastmoney", "sina"),
                )
            return super().daily_bars(symbol, start_date, end_date)

    builder = AKShareCanonicalBundleBuilder(
        provider=PartiallyUnavailableProvider(trading_date),
        clock=lambda: datetime(2026, 7, 15, 20, tzinfo=SHANGHAI),
        bundle_size=20,
        history_sessions=65,
    )
    bundle = builder.build(trading_date, datetime(2026, 7, 15, 18, tzinfo=SHANGHAI))
    assert len(bundle.securities) == 20
    assert all(item.symbol != "600000.SH" for item in bundle.securities)
    assert builder.acquisition_events == [
        {
            "operation": "daily_bars",
            "subject": "600000.SH",
            "attempt_count": 4,
            "sources": ["eastmoney", "sina"],
            "outcome": "skipped_nonessential_symbol",
        }
    ]


def test_bundle_fails_closed_when_all_benchmark_sources_fail() -> None:
    trading_date = date(2026, 7, 15)

    class MissingBenchmarkProvider(Provider):
        def benchmark_bars(self, code, start_date, end_date):
            del start_date, end_date
            raise MarketDataAcquisitionError(
                operation="benchmark_bars",
                subject=code,
                attempt_count=4,
                sources=("eastmoney", "sina"),
            )

    builder = AKShareCanonicalBundleBuilder(
        provider=MissingBenchmarkProvider(trading_date),
        clock=lambda: datetime(2026, 7, 15, 20, tzinfo=SHANGHAI),
        bundle_size=20,
        history_sessions=65,
    )
    try:
        builder.build(trading_date, datetime(2026, 7, 15, 18, tzinfo=SHANGHAI))
    except MarketDataAcquisitionError as exc:
        assert exc.operation == "benchmark_bars"
        assert "sensitive" not in str(exc)
        assert "000300" in str(exc)
    else:
        raise AssertionError("benchmark acquisition must fail closed")


def test_canonical_provider_falls_back_for_missing_history_and_tracks_source() -> None:
    trading_date = date(2026, 7, 15)

    class Primary(Provider):
        source = "akshare"

        def securities(self):
            return [
                *super().securities(),
                {
                    "代码": "601998",
                    "名称": "*ST承接样本",
                    "最新价": 5,
                    "成交额": 50_000_000,
                },
            ]

        def daily_bars(self, symbol, start_date, end_date):
            del symbol, start_date, end_date
            return []

        def benchmark_bars(self, code, start_date, end_date):
            del code, start_date, end_date
            return []

    class Fallback(Provider):
        source = "tushare"

        def daily_bars(self, symbol, start_date, end_date):
            rows = super().daily_bars(symbol, start_date, end_date)
            return rows[:-5] if symbol == "601999.SH" else rows

    provider = FallbackCanonicalProvider(
        Primary(trading_date),
        Fallback(trading_date),
        minimum_history_rows=65,
    )
    builder = AKShareCanonicalBundleBuilder(
        provider=provider,
        clock=lambda: datetime(2026, 7, 15, 20, tzinfo=SHANGHAI),
        bundle_size=20,
        history_sessions=65,
    )
    bundle = builder.build(
        trading_date,
        datetime(2026, 7, 15, 18, tzinfo=SHANGHAI),
        required_symbols=("601999.SH", "601998.SH"),
    )
    assert any(item.symbol == "601999.SH" for item in bundle.securities)
    tracked_status = next(item for item in bundle.statuses if item.symbol == "601999.SH")
    assert tracked_status.is_suspended is True
    assert bundle.data_quality["601999.SH"]["tracked_only"] is True
    assert (
        max(item.trading_date for item in bundle.bars if item.symbol == "601999.SH") < trading_date
    )
    st_status = next(item for item in bundle.statuses if item.symbol == "601998.SH")
    st_master = next(item for item in bundle.securities if item.symbol == "601998.SH")
    assert st_master.short_name == "*ST承接样本"
    assert st_status.is_st is True
    assert st_status.is_suspended is False
    assert {item.source for item in bundle.securities if item.symbol != "601999.SH"} == {"akshare"}
    assert {item.source for item in bundle.bars} == {"tushare"}
    assert {item["source"] for item in bundle.data_quality.values()} == {"tushare"}


def test_run_manifest_freezes_acquisition_plan_without_calling_akshare(tmp_path) -> None:
    class Builder:
        def build(self, trading_date, decision_at):
            del trading_date, decision_at
            raise AssertionError("HTTP submission must not fetch AKShare")

    backend = BuiltinDailyBackend(
        object_root=tmp_path / "objects",
        state_root=tmp_path / "state",
        policy_path="configs/first_release.v1.json",
        canonical_builder=Builder(),  # type: ignore[arg-type]
    )
    backend._settings = Settings(
        canonical_bundle_mode="akshare",
        agent_backend="builtin",
    )
    manifest = backend.run_manifest(
        date(2026, 7, 15),
        datetime(2026, 7, 15, 18, tzinfo=SHANGHAI),
    )
    assert manifest["canonical_source_mode"] == "akshare"
    assert "acquired_bundle_sha256" not in manifest


def _weekdays_ending(value: date, count: int) -> list[date]:
    result = []
    cursor = value
    while len(result) < count:
        if cursor.weekday() < 5:
            result.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(result))
