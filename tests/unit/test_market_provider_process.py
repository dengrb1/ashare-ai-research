from __future__ import annotations

import json
import queue
import threading
import time
from datetime import date, datetime
from typing import Any

import pytest

from ashare_ai.market.akshare_worker import handle_request
from ashare_ai.market.service import (
    AKShareMarketProvider,
    MarketDataService,
    _AKShareInProcessProvider,
    _number,
    _PriorityGate,
)


class _Output:
    def __init__(self) -> None:
        self.lines: queue.Queue[str] = queue.Queue()
        self.lines.put('{"ready":true}\n')

    def readline(self, _limit: int) -> str:
        return self.lines.get(timeout=2)


class _Input:
    def __init__(self, process: _Process) -> None:
        self.process = process

    def write(self, value: str) -> int:
        envelope = json.loads(value)
        self.process.requests.append(envelope)
        if self.process.respond:
            symbol = envelope["payload"].get("symbols", ["600519.SH"])[0]
            self.process.stdout.lines.put(
                json.dumps(
                    {
                        "id": envelope["id"] + self.process.response_id_offset,
                        "ok": True,
                        "items": [{"symbol": symbol, "price": 100.0}],
                    }
                )
                + "\n"
            )
        return len(value)

    def flush(self) -> None:
        return None


class _Process:
    def __init__(self, *, respond: bool = True, response_id_offset: int = 0) -> None:
        self.respond = respond
        self.response_id_offset = response_id_offset
        self.stdout = _Output()
        self.stdin = _Input(self)
        self.requests: list[dict[str, Any]] = []
        self.returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15
        self.stdout.lines.put("")

    def wait(self, timeout: float) -> int:
        del timeout
        return self.returncode or 0

    def kill(self) -> None:
        self.returncode = -9


def test_parent_provider_reuses_bounded_non_shell_process(monkeypatch) -> None:
    processes: list[_Process] = []
    observed: list[tuple[list[str], dict[str, Any]]] = []

    def popen(command: list[str], **kwargs: Any) -> _Process:
        observed.append((command, kwargs))
        process = _Process()
        processes.append(process)
        return process

    monkeypatch.setenv("ADMIN_PASSWORD", "must-not-reach-market-child")
    monkeypatch.setattr("ashare_ai.market.service.subprocess.Popen", popen)
    provider = AKShareMarketProvider(timeout_seconds=3)
    assert provider.quotes(["600519.sh"]) == [
        {"symbol": "600519.SH", "price": 100.0}
    ]
    assert provider.quotes(["000001.sz"])[0]["symbol"] == "000001.SZ"

    assert len(processes) == 1
    assert observed[0][0][1:] == ["-m", "ashare_ai.market.akshare_worker"]
    assert "shell" not in observed[0][1]
    assert "ADMIN_PASSWORD" not in observed[0][1]["env"]
    assert observed[0][1]["close_fds"] is True
    assert [item["id"] for item in processes[0].requests] == [1, 2]
    provider.close()
    assert processes[0].terminated is True


def test_parent_provider_timeout_reaps_child_and_restarts(monkeypatch) -> None:
    processes = [_Process(respond=False), _Process()]
    monkeypatch.setattr(
        "ashare_ai.market.service.subprocess.Popen", lambda *_args, **_kwargs: processes.pop(0)
    )
    provider = AKShareMarketProvider(timeout_seconds=3)
    provider.timeout_seconds = 0.02
    first = processes[0]
    with pytest.raises(TimeoutError, match="provider timed out"):
        provider.quotes(["600519.SH"])
    assert first.terminated is True
    assert provider.quotes(["600519.SH"])[0]["price"] == 100.0


def test_parent_provider_reaps_protocol_mismatch(monkeypatch) -> None:
    process = _Process(response_id_offset=1)
    monkeypatch.setattr(
        "ashare_ai.market.service.subprocess.Popen", lambda *_args, **_kwargs: process
    )
    provider = AKShareMarketProvider(timeout_seconds=2)

    with pytest.raises(RuntimeError, match="invalid response"):
        provider.quotes(["600519.SH"])

    assert process.terminated is True
    assert provider.state == "DEGRADED"


def test_foreground_request_overtakes_queued_prefetch() -> None:
    gate = _PriorityGate()
    gate.acquire(background=True)
    order: list[str] = []
    background_waiting = threading.Event()
    foreground_waiting = threading.Event()

    def waiter(name: str, *, background: bool, waiting: threading.Event) -> None:
        waiting.set()
        gate.acquire(background=background)
        order.append(name)
        gate.release()

    background = threading.Thread(
        target=waiter,
        args=("background",),
        kwargs={"background": True, "waiting": background_waiting},
    )
    foreground = threading.Thread(
        target=waiter,
        args=("foreground",),
        kwargs={"background": False, "waiting": foreground_waiting},
    )
    background.start()
    background_waiting.wait(timeout=1)
    foreground.start()
    foreground_waiting.wait(timeout=1)
    time.sleep(0.01)
    gate.release()
    background.join(timeout=1)
    foreground.join(timeout=1)

    assert order == ["foreground", "background"]


def test_child_contract_filters_quotes_and_validates_kline_limit() -> None:
    class Provider:
        def quotes(self, symbols: list[str]) -> list[dict[str, Any]]:
            assert symbols == ["600519.SH"]
            return [
                {"symbol": "600519.SH", "price": 100.0},
                {"symbol": "000001.SZ", "price": 10.0},
            ]

        def klines(
            self,
            symbol: str,
            period: str,
            start: datetime | None,
            end: datetime | None,
            limit: int,
        ) -> list[dict[str, Any]]:
            del symbol, period, start, end, limit
            return []

    rows = handle_request(
        {"operation": "quotes", "symbols": ["600519.sh"]}, provider=Provider()  # type: ignore[arg-type]
    )
    assert rows == [{"symbol": "600519.SH", "price": 100.0}]
    with pytest.raises(ValueError, match="kline limit"):
        handle_request(
            {
                "operation": "klines",
                "symbol": "600519.SH",
                "period": "daily",
                "limit": 5001,
            },
            provider=Provider(),  # type: ignore[arg-type]
        )


def test_child_contract_returns_bounded_calendar_rows() -> None:
    class Provider:
        def sessions(self, start_date: date, end_date: date) -> list[date]:
            assert start_date == date(2026, 7, 17)
            assert end_date == date(2026, 7, 20)
            return [date(2026, 7, 17), date(2026, 7, 20)]

    rows = handle_request(
        {
            "operation": "sessions",
            "start": "2026-07-17",
            "end": "2026-07-20",
        },
        provider=Provider(),  # type: ignore[arg-type]
    )
    assert rows == [{"date": "2026-07-17"}, {"date": "2026-07-20"}]


def test_market_number_rejects_non_finite_values() -> None:
    assert _number(float("nan")) is None
    assert _number(float("inf")) is None
    assert _number(float("-inf")) is None


def test_in_process_daily_klines_fall_back_to_sina_when_eastmoney_disconnects(
    monkeypatch,
) -> None:
    class Frame:
        def tail(self, limit: int) -> Frame:
            assert limit == 10
            return self

        def to_dict(self, *, orient: str) -> list[dict[str, Any]]:
            assert orient == "records"
            return [
                {
                    "date": date(2026, 8, 6),
                    "open": 9.28,
                    "high": 9.35,
                    "low": 9.16,
                    "close": 9.29,
                    "volume": 67232905,
                    "amount": 621683353,
                    "turnover": 0.002,
                }
            ]

    class SDK:
        def stock_zh_a_hist(self, **_kwargs: Any) -> None:
            raise ConnectionError("upstream reset")

        def stock_zh_a_daily(self, **kwargs: Any) -> Frame:
            assert kwargs["symbol"] == "sh600000"
            assert kwargs["adjust"] == ""
            return Frame()

    provider = _AKShareInProcessProvider()
    monkeypatch.setattr(provider, "_sdk", lambda: SDK())

    rows = provider.klines("600000.SH", "daily", None, None, 10, "raw")

    assert rows[0]["timestamp"].startswith("2026-08-06")
    assert rows[0]["close"] == 9.29


def test_calendar_uses_isolated_retry_after_primary_failure(monkeypatch) -> None:
    class Primary:
        source = "akshare"

        def sessions(self, _start: date, _end: date) -> tuple[date, ...]:
            raise RuntimeError("provider returned an error")

    primary = Primary()
    service = MarketDataService(primary=primary, redis_client=None)  # type: ignore[arg-type]
    expected = (date(2026, 8, 6),)

    class Isolated:
        def __init__(self, *, timeout_seconds: float) -> None:
            assert timeout_seconds > 0

        def sessions(self, _start: date, _end: date) -> tuple[date, ...]:
            return expected

        def close(self) -> None:
            return None

    monkeypatch.setattr("ashare_ai.market.service.AKShareMarketProvider", Isolated)

    assert service._fetch_calendar() == expected
