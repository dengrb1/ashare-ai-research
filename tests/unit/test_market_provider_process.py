from __future__ import annotations

import json
import subprocess
from datetime import datetime
from typing import Any

import pytest

from ashare_ai.market.akshare_worker import handle_request
from ashare_ai.market.service import AKShareMarketProvider, _number


def test_parent_provider_uses_bounded_non_shell_process(monkeypatch) -> None:
    observed: dict[str, Any] = {}

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        observed.update({"command": command, **kwargs})
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "ok": True,
                    "items": [{"symbol": "600519.SH", "price": 100.0}],
                }
            ),
            stderr="",
        )

    monkeypatch.setattr("ashare_ai.market.service.subprocess.run", run)
    rows = AKShareMarketProvider(timeout_seconds=3).quotes(["600519.sh"])

    assert rows == [{"symbol": "600519.SH", "price": 100.0}]
    assert observed["command"][1:] == ["-m", "ashare_ai.market.akshare_worker"]
    assert "shell" not in observed
    assert observed["timeout"] == 2.5
    assert json.loads(str(observed["input"])) == {
        "operation": "quotes",
        "symbols": ["600519.SH"],
    }


def test_parent_provider_hides_child_error_and_enforces_timeout(monkeypatch) -> None:
    def failed(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 1, stdout="", stderr="secret-token")

    monkeypatch.setattr("ashare_ai.market.service.subprocess.run", failed)
    with pytest.raises(RuntimeError, match="provider process failed") as exc_info:
        AKShareMarketProvider(timeout_seconds=3).quotes(["600519.SH"])
    assert "secret-token" not in str(exc_info.value)

    def timed_out(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired([], 2.5)

    monkeypatch.setattr("ashare_ai.market.service.subprocess.run", timed_out)
    with pytest.raises(TimeoutError, match="provider timed out"):
        AKShareMarketProvider(timeout_seconds=3).quotes(["600519.SH"])


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


def test_market_number_rejects_non_finite_values() -> None:
    assert _number(float("nan")) is None
    assert _number(float("inf")) is None
    assert _number(float("-inf")) is None
