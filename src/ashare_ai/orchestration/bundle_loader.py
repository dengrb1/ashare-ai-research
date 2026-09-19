"""
Bundle 加载工具。

从磁盘加载 CanonicalDailyBundle 用于 CLI 和 API。
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from ashare_ai.orchestration.bundle import CanonicalDailyBundle

logger = logging.getLogger(__name__)


def load_bundle_from_disk(
    bundle_dir: Path,
    trading_date: date,
) -> CanonicalDailyBundle:
    """
    从磁盘加载指定日期的 Bundle。

    Args:
        bundle_dir: Bundle 存储目录
        trading_date: 交易日期

    Returns:
        CanonicalDailyBundle: 加载的 Bundle

    Raises:
        FileNotFoundError: Bundle 文件不存在
        ValueError: Bundle 格式错误
    """
    # Bundle 文件路径格式：bundle_dir / YYYY-MM-DD.bundle.json
    bundle_file = bundle_dir / f"{trading_date}.bundle.json"

    if not bundle_file.exists():
        raise FileNotFoundError(f"Bundle not found: {bundle_file}")

    logger.info(f"Loading bundle from {bundle_file}")

    # TODO: 实现实际加载逻辑
    # 1. 读取 JSON 文件
    # 2. 反序列化为 CanonicalDailyBundle
    # 3. 验证数据完整性

    import json

    with bundle_file.open("r", encoding="utf-8") as f:
        data = json.load(f)

    bundle = CanonicalDailyBundle.model_validate(data)
    logger.info(f"Loaded bundle: {len(bundle.securities)} securities")

    return bundle


def list_available_bundles(bundle_dir: Path) -> list[date]:
    """
    列出可用的 Bundle 日期。

    Args:
        bundle_dir: Bundle 存储目录

    Returns:
        list[date]: 可用的交易日期列表（升序）
    """
    if not bundle_dir.exists():
        return []

    dates: list[date] = []

    for bundle_file in bundle_dir.glob("*.bundle.json"):
        try:
            date_str = bundle_file.stem.split(".")[0]  # YYYY-MM-DD.bundle.json -> YYYY-MM-DD
            trading_date = date.fromisoformat(date_str)
            dates.append(trading_date)
        except (ValueError, IndexError) as e:
            logger.warning(f"Invalid bundle filename: {bundle_file.name} - {e}")
            continue

    return sorted(dates)


def find_latest_bundle(bundle_dir: Path, before: date | None = None) -> date | None:
    """
    查找最新的 Bundle 日期。

    Args:
        bundle_dir: Bundle 存储目录
        before: 在此日期之前（不包括）

    Returns:
        date | None: 最新的交易日期，如果没有返回 None
    """
    dates = list_available_bundles(bundle_dir)

    if not dates:
        return None

    if before is not None:
        dates = [d for d in dates if d < before]

    return dates[-1] if dates else None
