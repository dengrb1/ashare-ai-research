"""
Jev 模型数据集生成器。

从 CanonicalDailyBundle 生成多任务学习数据集，支持：
- 时序特征提取
- 多任务标签生成（方向、涨幅、动作、风险、仓位）
- Walk-forward 时序切分
- PIT 约束保证
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import numpy as np
from pydantic import BaseModel

if TYPE_CHECKING:
    from ashare_ai.orchestration.bundle import CanonicalDailyBundle

logger = logging.getLogger(__name__)


class LabelWindow(NamedTuple):
    """标签窗口配置"""

    horizon_1d: int = 1  # 1日方向
    horizon_5d: int = 5  # 5日方向和涨幅
    threshold_3pct: float = 0.03  # 涨幅阈值


class DatasetSample(BaseModel):
    """单个训练样本"""

    symbol: str
    trading_date: date
    features: dict[str, float]  # 扁平化特征
    labels: dict[str, int | float]  # 多任务标签


class JevDatasetConfig(BaseModel):
    """数据集配置"""

    bundle_dir: Path
    output_dir: Path
    train_start: date
    train_end: date
    val_start: date
    val_end: date
    test_start: date
    test_end: date
    label_window: LabelWindow = LabelWindow()
    min_history_days: int = 60


class JevDatasetGenerator:
    """Jev 模型数据集生成器"""

    def __init__(self, config: JevDatasetConfig):
        self.config = config
        self.config.output_dir.mkdir(parents=True, exist_ok=True)

    def generate_dataset(self) -> dict[str, Path]:
        """
        生成完整数据集。

        Returns:
            dict[str, Path]: 数据集文件路径（train, val, test）
        """
        logger.info("Generating Jev dataset...")

        train_path = self.config.output_dir / "train.parquet"
        val_path = self.config.output_dir / "val.parquet"
        test_path = self.config.output_dir / "test.parquet"

        from ashare_ai.orchestration.bundle_loader import load_bundle_from_disk, list_available_bundles

        bundles = []
        for trading_date in list_available_bundles(self.config.bundle_dir):
            if trading_date > self.config.test_end:
                continue
            try:
                bundles.append(load_bundle_from_disk(self.config.bundle_dir, trading_date))
            except (OSError, ValueError) as exc:
                logger.warning("skip invalid bundle %s: %s", trading_date, type(exc).__name__)
        by_symbol: dict[str, list[CanonicalDailyBundle]] = {}
        for bundle in bundles:
            for security in bundle.securities:
                by_symbol.setdefault(str(security.symbol), []).append(bundle)
        samples: list[DatasetSample] = []
        for symbol, history in by_symbol.items():
            history.sort(key=lambda item: item.trading_date)
            for index, bundle in enumerate(history):
                labels = self.generate_labels(history, symbol, bundle.trading_date)
                if labels is None or index + 1 < self.config.min_history_days:
                    continue
                samples.append(DatasetSample(symbol=symbol, trading_date=bundle.trading_date, features=self.extract_features(bundle, symbol), labels=labels))
        train, val, test = self.time_series_split(self.apply_pit_constraints(samples))
        for path, rows in ((train_path, train), (val_path, val), (test_path, test)):
            path.write_text("\n".join(item.model_dump_json() for item in rows) + ("\n" if rows else ""), encoding="utf-8")

        return {
            "train": train_path,
            "val": val_path,
            "test": test_path,
        }

    def extract_features(self, bundle: CanonicalDailyBundle, symbol: str) -> dict[str, float]:
        """
        从 Bundle 提取特征。

        Args:
            bundle: 市场数据 Bundle
            symbol: 股票代码

        Returns:
            dict[str, float]: 扁平化特征字典
        """
        features: dict[str, float] = {}

        bars = [bar for bar in bundle.bars if str(bar.symbol) == symbol and bar.trading_date <= bundle.trading_date]
        if not bars:
            return {"close": 0.0, "volume": 0.0}
        bar = max(bars, key=lambda item: item.trading_date)
        features.update({"open": float(bar.open), "high": float(bar.high), "low": float(bar.low), "close": float(bar.close), "volume": float(bar.volume), "amount": float(bar.amount)})
        features["return_1d"] = float((bar.close / bar.prev_close) - 1) if bar.prev_close else 0.0

        return features

    def generate_labels(
        self,
        bundles: Sequence[CanonicalDailyBundle],
        symbol: str,
        current_date: date,
    ) -> dict[str, int | float] | None:
        """
        生成多任务标签。

        Args:
            bundles: 时序 Bundle 序列（已排序）
            symbol: 股票代码
            current_date: 当前日期

        Returns:
            dict[str, int | float] | None: 标签字典，如果未来数据不足则返回 None
        """
        bars = sorted((bar for bundle in bundles for bar in bundle.bars if str(bar.symbol) == symbol), key=lambda item: item.trading_date)
        try:
            index = next(i for i, bar in enumerate(bars) if bar.trading_date == current_date)
            current = float(bars[index].close)
            future_1d = float(bars[index + self.config.label_window.horizon_1d].close)
            future_5d = float(bars[index + self.config.label_window.horizon_5d].close)
        except (StopIteration, IndexError, ZeroDivisionError):
            return None
        return {"direction_1d": 2 if future_1d / current - 1 > 0.01 else 0 if future_1d / current - 1 < -0.01 else 1, "direction_5d": 2 if future_5d / current - 1 > 0.02 else 0 if future_5d / current - 1 < -0.02 else 1, "up_over_3pct_5d": int(future_5d / current - 1 >= self.config.label_window.threshold_3pct), "action": 2 if future_5d / current - 1 > 0.03 else 0 if future_5d / current - 1 < -0.03 else 1, "risk": 2 if abs(future_5d / current - 1) > 0.12 else 1 if abs(future_5d / current - 1) > 0.06 else 0, "position": 50 if future_5d / current - 1 > 0.03 else 0}

    def apply_pit_constraints(self, samples: list[DatasetSample]) -> list[DatasetSample]:
        """
        应用 PIT（Point-in-Time）约束。

        确保特征的 available_at 早于 trading_date，防止未来信息泄漏。

        Args:
            samples: 原始样本列表

        Returns:
            list[DatasetSample]: 过滤后的样本列表
        """
        return [sample for sample in samples if sample.trading_date >= self.config.train_start and not (sample.features.get("available_at_epoch", float("-inf")) > sample.trading_date.toordinal())]

    def time_series_split(
        self, samples: list[DatasetSample]
    ) -> tuple[list[DatasetSample], list[DatasetSample], list[DatasetSample]]:
        """
        时序切分数据集（walk-forward）。

        Args:
            samples: 所有样本

        Returns:
            tuple: (train, val, test) 样本列表
        """
        train = [s for s in samples if self.config.train_start <= s.trading_date <= self.config.train_end]
        val = [s for s in samples if self.config.val_start <= s.trading_date <= self.config.val_end]
        test = [s for s in samples if self.config.test_start <= s.trading_date <= self.config.test_end]

        logger.info(f"Time series split: train={len(train)}, val={len(val)}, test={len(test)}")

        return train, val, test
