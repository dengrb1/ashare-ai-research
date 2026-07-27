from datetime import date, datetime

from ashare_ai.core.time import SHANGHAI, market_session


def test_market_session_distinguishes_open_break_and_final_close() -> None:
    sessions = {date(2026, 7, 20)}

    assert market_session(datetime(2026, 7, 20, 10, tzinfo=SHANGHAI), sessions).state == "OPEN"
    assert market_session(datetime(2026, 7, 20, 11, 30, tzinfo=SHANGHAI), sessions).state == "BREAK"
    closed = market_session(datetime(2026, 7, 20, 15, tzinfo=SHANGHAI), sessions)
    assert (closed.state, closed.is_trading_day, closed.reason) == (
        "CLOSED",
        True,
        "AFTER_CLOSE",
    )


def test_market_session_marks_non_trading_days_closed_and_calendar_failures_unknown() -> None:
    weekend = market_session(datetime(2026, 7, 19, 10, tzinfo=SHANGHAI), set())
    unavailable = market_session(datetime(2026, 7, 20, 10, tzinfo=SHANGHAI), None)

    assert (weekend.state, weekend.is_trading_day, weekend.reason) == (
        "CLOSED",
        False,
        "NON_TRADING_DAY",
    )
    assert (unavailable.state, unavailable.is_trading_day, unavailable.reason) == (
        "UNKNOWN",
        None,
        "CALENDAR_UNAVAILABLE",
    )
