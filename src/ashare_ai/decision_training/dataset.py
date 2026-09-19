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

        # TODO: 实现数据集生成逻辑
        # 1. 扫描 bundle_dir，加载 CanonicalDailyBundle
        # 2. 提取特征和标签
        # 3. 应用 PIT 约束
        # 4. 时序切分
        # 5. 保存为 parquet/npz

        train_path = self.config.output_dir / "train.parquet"
        val_path = self.config.output_dir / "val.parquet"
        test_path = self.config.output_dir / "test.parquet"

        logger.warning("JevDatasetGenerator.generate_dataset() not yet implemented")

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

        # TODO: 提取技术特征（OHLCV、均线、动量等）
        # TODO: 提取基本面特征（估值、盈利、成长等）
        # TODO: 提取情绪特征（新闻、公告、市场情绪等）

        # 占位符
        features["close"] = 0.0
        features["volume"] = 0.0

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
        # TODO: 实现标签生成
        # 1. 找到未来 1d/5d 的收盘价
        # 2. 计算方向（UP=2, FLAT=1, DOWN=0）
        # 3. 计算涨幅是否超过 3%（YES=1, NO=0）
        # 4. 映射到动作（BUY=2, HOLD=1, SELL=0）
        # 5. 映射到风险（LOW=0, MEDIUM=1, HIGH=2）
        # 6. 映射到仓位（0/10/20/30/50/70/100）

        # 占位符
        return None

    def apply_pit_constraints(self, samples: list[DatasetSample]) -> list[DatasetSample]:
        """
        应用 PIT（Point-in-Time）约束。

        确保特征的 available_at 早于 trading_date，防止未来信息泄漏。

        Args:
            samples: 原始样本列表

        Returns:
            list[DatasetSample]: 过滤后的样本列表
        """
        # TODO: 实现 PIT 约束检查
        logger.warning("PIT constraint check not yet implemented")
        return samples

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
