from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

from ashare_ai.core.contracts import (
    AvailabilityBasis,
    Board,
    DailyBar,
    Exchange,
    SecurityMasterRecord,
    SecurityStatusRecord,
)
from ashare_ai.universe import EligibilityReason, UniverseConfig, build_dynamic_universe

SHANGHAI = ZoneInfo("Asia/Shanghai")
HASH = "c" * 64
TRADING_DATE = date(2026, 7, 15)
DECISION_AT = datetime(2026, 7, 15, 18, tzinfo=SHANGHAI)


def test_dynamic_universe_filters_a_share_risks_and_missing_data() -> None:
    symbols = {
        "eligible": "600000.SH",
        "st": "000001.SZ",
        "suspended": "300001.SZ",
        "new": "688001.SH",
        "illiquid": "430047.BJ",
        "volatile": "600001.SH",
        "missing": "600002.SH",
    }
    masters = [
        _master(symbol, list_date=TRADING_DATE - timedelta(days=200)) for symbol in symbols.values()
    ]
    masters[3] = _master(symbols["new"], list_date=TRADING_DATE - timedelta(days=2))
    statuses = [
        _status(symbols["eligible"]),
        _status(symbols["st"], is_st=True),
        _status(symbols["suspended"], is_suspended=True),
        _status(symbols["new"]),
        _status(symbols["illiquid"]),
        _status(symbols["volatile"]),
        _status(symbols["missing"]),
    ]
    bars: list[DailyBar] = []
    for name, symbol in symbols.items():
        count = 2 if name == "missing" else 8
        amount = Decimal("100") if name == "illiquid" else Decimal("100000")
        bars.extend(_bars(symbol, count=count, amount=amount, volatile=name == "volatile"))

    result = build_dynamic_universe(
        masters,
        statuses,
        bars,
        trading_date=TRADING_DATE,
        decision_at=DECISION_AT,
        config=UniverseConfig(
            min_listing_days=10,
            liquidity_window=5,
            min_average_amount=1000,
            min_history_days=5,
            max_abs_daily_return=0.25,
            max_annualized_volatility=5,
        ),
    )
    decisions = {decision.symbol: decision for decision in result.decisions}
    assert result.included == (symbols["eligible"],)
    assert decisions[symbols["eligible"]].reasons == (EligibilityReason.ELIGIBLE,)
    assert EligibilityReason.ST in decisions[symbols["st"]].reasons
    assert EligibilityReason.SUSPENDED in decisions[symbols["suspended"]].reasons
    assert EligibilityReason.TOO_NEW in decisions[symbols["new"]].reasons
    assert EligibilityReason.LOW_LIQUIDITY in decisions[symbols["illiquid"]].reasons
    assert EligibilityReason.ABNORMAL_VOLATILITY in decisions[symbols["volatile"]].reasons
    assert EligibilityReason.INSUFFICIENT_HISTORY in decisions[symbols["missing"]].reasons


def test_universe_ignores_future_status_revision() -> None:
    symbol = "600000.SH"
    future_st = _status(symbol, is_st=True).model_copy(
        update={
            "available_at": DECISION_AT + timedelta(seconds=1),
            "fetched_at": DECISION_AT + timedelta(seconds=1),
            "source_record_id": "future-st",
        }
    )
    result = build_dynamic_universe(
        [_master(symbol, list_date=TRADING_DATE - timedelta(days=200))],
        [_status(symbol), future_st],
        _bars(symbol, count=8, amount=Decimal("100000")),
        trading_date=TRADING_DATE,
        decision_at=DECISION_AT,
        config=UniverseConfig(
            min_listing_days=10,
            liquidity_window=5,
            min_average_amount=1000,
            min_history_days=5,
        ),
    )
    assert result.included == (symbol,)


def test_old_abnormal_return_does_not_permanently_pollute_universe() -> None:
    symbol = "600000.SH"
    bars = _bars(symbol, count=10, amount=Decimal("100000"))
    prior_close = bars[1].prev_close
    assert prior_close is not None
    abnormal_close = prior_close * Decimal("1.40")
    bars[1] = bars[1].model_copy(
        update={
            "close": abnormal_close,
            "high": abnormal_close,
        }
    )
    result = build_dynamic_universe(
        [_master(symbol, list_date=TRADING_DATE - timedelta(days=200))],
        [_status(symbol)],
        bars,
        trading_date=TRADING_DATE,
        decision_at=DECISION_AT,
        config=UniverseConfig(
            min_listing_days=10,
            liquidity_window=5,
            min_average_amount=1000,
            min_history_days=5,
            abnormal_return_window=3,
            max_abs_daily_return=0.25,
            max_annualized_volatility=5,
        ),
    )
    assert result.included == (symbol,)


def _master(symbol: str, *, list_date: date) -> SecurityMasterRecord:
    exchange = Exchange(symbol[-2:])
    board = Board.BSE if exchange == Exchange.BJ else Board.MAIN
    return SecurityMasterRecord(
        **_pit(symbol, "master"),
        exchange=exchange,
        board=board,
        short_name="fixture",
        list_date=list_date,
        effective_from=list_date,
    )


def _status(
    symbol: str, *, is_st: bool = False, is_suspended: bool = False
) -> SecurityStatusRecord:
    return SecurityStatusRecord(
        **_pit(symbol, "status"),
        is_st=is_st,
        is_suspended=is_suspended,
        effective_from=TRADING_DATE - timedelta(days=30),
    )


def _bars(symbol: str, *, count: int, amount: Decimal, volatile: bool = False) -> list[DailyBar]:
    records: list[DailyBar] = []
    previous = Decimal("10")
    for offset in range(count - 1, -1, -1):
        current_date = TRADING_DATE - timedelta(days=offset)
        close = previous * Decimal("1.01")
        if volatile and offset == 0:
            close = previous * Decimal("1.4")
        records.append(
            DailyBar(
                **_pit(symbol, f"bar-{current_date}", trading_date=current_date),
                open=previous,
                high=max(previous, close),
                low=min(previous, close),
                close=close,
                prev_close=previous,
                volume=Decimal("10000"),
                amount=amount,
            )
        )
        previous = close
    return records


def _pit(
    symbol: str, source_record_id: str, *, trading_date: date = TRADING_DATE
) -> dict[str, object]:
    available_at = datetime.combine(
        trading_date, datetime.min.time().replace(hour=17), tzinfo=SHANGHAI
    )
    return {
        "symbol": symbol,
        "trading_date": trading_date,
        "available_at": available_at,
        "source": "fixture",
        "source_record_id": source_record_id,
        "fetched_at": available_at,
        "payload_sha256": HASH,
        "adapter_version": "test",
        "ingestion_run_id": uuid4(),
        "availability_basis": AvailabilityBasis.VENDOR_TIMESTAMP,
    }
