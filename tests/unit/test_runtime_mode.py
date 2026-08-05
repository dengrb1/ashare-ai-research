from datetime import datetime

from ashare_ai.core.config import Settings
from ashare_ai.core.runtime_mode import is_after_close, runtime_mode_policy
from ashare_ai.core.time import SHANGHAI
from ashare_ai.market.service import AKShareMarketProvider, MarketDataService, SinaMarketProvider


def test_lightweight_profile_avoids_heavy_primary_and_bounds_runtime_resources() -> None:
    settings = Settings(_env_file=None, api_runtime_mode="LIGHTWEIGHT")
    policy = runtime_mode_policy(settings)
    service = MarketDataService(settings=settings, redis_client=None)
    try:
        assert isinstance(service.primary, SinaMarketProvider)
        assert policy.memory_strategy == "LOW_RESIDENT"
        assert policy.market_cache_max_entries == 128
        assert policy.market_prefetch_max_workers == 1
        assert policy.market_provider_max_workers == 1
    finally:
        service.close()


def test_supreme_profile_keeps_isolated_provider_and_configured_capacity() -> None:
    settings = Settings(
        _env_file=None,
        api_runtime_mode="SUPREME",
        market_cache_max_entries=256,
        market_prefetch_max_workers=3,
        market_provider_max_workers=2,
        market_provider_max_queue=5,
    )
    policy = runtime_mode_policy(settings)
    service = MarketDataService(settings=settings, redis_client=None)
    try:
        assert isinstance(service.primary, AKShareMarketProvider)
        assert policy.memory_strategy == "MAX_THROUGHPUT"
        assert policy.market_cache_max_entries == 256
        assert policy.market_prefetch_max_workers == 3
        assert policy.market_provider_max_workers == 2
        assert policy.market_provider_max_queue == 5
    finally:
        service.close()


def test_after_close_hint_is_calendar_free_and_timezone_aware() -> None:
    assert is_after_close(datetime(2026, 7, 20, 15, 0, tzinfo=SHANGHAI)) is True
    assert is_after_close(datetime(2026, 7, 20, 14, 59, tzinfo=SHANGHAI)) is False
    assert is_after_close(datetime(2026, 7, 19, 10, tzinfo=SHANGHAI)) is True
