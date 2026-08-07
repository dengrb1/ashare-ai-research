from __future__ import annotations

from datetime import date
from importlib import import_module
from types import SimpleNamespace

from pytest import MonkeyPatch


def test_after_close_releases_caches_and_reclaims_process_memory(
    monkeypatch: MonkeyPatch,
) -> None:
    api_module = import_module("ashare_ai.api.app")
    events: list[object] = []

    class Market:
        def release_after_close(self) -> None:
            events.append("market-release")

    settings = SimpleNamespace()
    policy = SimpleNamespace(auto_close_after_close=True, mode="LIGHTWEIGHT")
    report = SimpleNamespace(
        attempted=True,
        reason="api-after-close",
        reclaimed_bytes=1024,
        collected_objects=3,
        allocator_trimmed=True,
    )
    monkeypatch.setattr(api_module, "get_effective_settings", lambda: settings)
    monkeypatch.setattr(api_module, "runtime_mode_policy", lambda _settings: policy)
    monkeypatch.setattr(api_module, "get_market_data_service", Market)
    monkeypatch.setattr(
        api_module,
        "_clear_financial_search_service",
        lambda: events.append("search-cache-clear"),
    )
    monkeypatch.setattr(api_module, "is_after_close", lambda _now=None: True)
    def reclaim_runtime_memory(actual: object, *, reason: str, force: bool = False) -> object:
        events.append(("reclaim", actual, reason, force))
        return report

    monkeypatch.setattr(api_module, "reclaim_runtime_memory", reclaim_runtime_memory)
    api_module._market_session_calendar_cache[date(2026, 8, 7)] = ()

    api_module._reconcile_market_runtime()

    assert events == [
        "market-release",
        "search-cache-clear",
        ("reclaim", settings, "api-after-close", False),
    ]
    assert api_module._market_session_calendar_cache == {}
