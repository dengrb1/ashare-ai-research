"""
异步 Bundle 加载器（性能优化版本）。

使用异步 I/O 避免阻塞事件循环。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import date
from functools import lru_cache
from pathlib import Path

from ashare_ai.orchestration.bundle import CanonicalDailyBundle

logger = logging.getLogger(__name__)


async def load_bundle_async(
    bundle_dir: Path,
    trading_date: date,
    verify_hash: bool = True,
) -> CanonicalDailyBundle:
    """
    异步加载 Bundle（性能优化版本）。

    Args:
        bundle_dir: Bundle 存储目录
        trading_date: 交易日期
        verify_hash: 是否验证文件哈希

    Returns:
        CanonicalDailyBundle: 加载的 Bundle

    Raises:
        FileNotFoundError: Bundle 文件不存在
        ValueError: Bundle 格式错误或哈希不匹配
    """
    bundle_file = bundle_dir / f"{trading_date}.bundle.json"
    metadata_file = bundle_dir / f"{trading_date}.bundle.meta.json"

    if not bundle_file.exists():
        raise FileNotFoundError(f"Bundle not found: {bundle_file}")

    logger.info(f"Loading bundle from {bundle_file}")

    # 异步读取文件
    loop = asyncio.get_event_loop()
    data_str = await loop.run_in_executor(None, bundle_file.read_text, "utf-8")

    # 验证哈希（如果有 metadata）
    if verify_hash and metadata_file.exists():
        metadata_str = await loop.run_in_executor(None, metadata_file.read_text, "utf-8")
        metadata = json.loads(metadata_str)
        expected_hash = metadata.get("sha256")

        if expected_hash:
            actual_hash = hashlib.sha256(data_str.encode("utf-8")).hexdigest()
            if actual_hash != expected_hash:
                raise ValueError(
                    f"Bundle hash mismatch: expected {expected_hash}, got {actual_hash}"
                )
            logger.debug(f"Bundle hash verified: {actual_hash[:8]}...")

    # 反序列化
    bundle = await loop.run_in_executor(
        None, lambda: CanonicalDailyBundle.model_validate_json(data_str)
    )

    logger.info(f"Loaded bundle: {len(bundle.securities)} securities")
    return bundle


@lru_cache(maxsize=20)
def _get_cached_bundle_sync(bundle_path: str, trading_date_str: str) -> CanonicalDailyBundle:
    """
    同步缓存加载（内部使用）。

    LRU 缓存最近 20 个 Bundle，避免重复加载。
    """
    from ashare_ai.orchestration.bundle_loader import load_bundle_from_disk

    return load_bundle_from_disk(Path(bundle_path), date.fromisoformat(trading_date_str))


async def load_bundle_cached(bundle_dir: Path, trading_date: date) -> CanonicalDailyBundle:
    """
    缓存版本的 Bundle 加载（推荐使用）。

    Args:
        bundle_dir: Bundle 存储目录
        trading_date: 交易日期

    Returns:
        CanonicalDailyBundle: 加载的 Bundle（可能来自缓存）
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None, _get_cached_bundle_sync, str(bundle_dir), trading_date.isoformat()
    )


async def batch_load_bundles(
    bundle_dir: Path,
    trading_dates: list[date],
    max_concurrent: int = 5,
) -> dict[date, CanonicalDailyBundle]:
    """
    批量并发加载多个 Bundle。

    Args:
        bundle_dir: Bundle 存储目录
        trading_dates: 交易日期列表
        max_concurrent: 最大并发数

    Returns:
        dict[date, CanonicalDailyBundle]: 日期到 Bundle 的映射
    """
    semaphore = asyncio.Semaphore(max_concurrent)

    async def load_with_semaphore(trading_date: date) -> tuple[date, CanonicalDailyBundle]:
        async with semaphore:
            bundle = await load_bundle_cached(bundle_dir, trading_date)
            return trading_date, bundle

    tasks = [load_with_semaphore(d) for d in trading_dates]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    bundles: dict[date, CanonicalDailyBundle] = {}
    for result in results:
        if isinstance(result, Exception):
            logger.error(f"Failed to load bundle: {result}")
        else:
            trading_date, bundle = result
            bundles[trading_date] = bundle

    return bundles


def clear_bundle_cache() -> None:
    """清空 Bundle 缓存"""
    _get_cached_bundle_sync.cache_clear()
    logger.info("Bundle cache cleared")
