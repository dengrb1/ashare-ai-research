"""
Jev 模型自动训练任务。

定期根据最新大盘走向和自选股 K 线数据自动训练 Jev 模型。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from ashare_ai.core.config import load_config
from ashare_ai.decision_training.dataset import JevDatasetConfig, JevDatasetGenerator, LabelWindow
from ashare_ai.decision_training.model import JevModelConfig, JevTrainer, JevTrainingConfig

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


async def schedule_jev_auto_training(
    db: Session,
    force: bool = False,
) -> dict[str, str]:
    """
    调度 Jev 模型自动训练任务。

    Args:
        db: 数据库会话
        force: 是否强制训练（忽略最近训练时间检查）

    Returns:
        dict: 训练任务状态
    """
    config = load_config()

    # 检查是否需要训练
    if not force:
        last_training = _get_last_training_time(config.jev_model_dir)
        if last_training and (datetime.now() - last_training).days < 7:
            logger.info(f"Jev model was trained recently ({last_training}), skipping")
            return {
                "status": "skipped",
                "reason": "trained_recently",
                "last_training": last_training.isoformat(),
            }

    # 准备数据集配置
    today = date.today()
    dataset_config = JevDatasetConfig(
        bundle_dir=config.lake_root / "canonical_daily_bundles",
        output_dir=config.lake_root / "jev_training_data" / today.isoformat(),
        train_start=today - timedelta(days=365 * 2),  # 2年训练集
        train_end=today - timedelta(days=60),
        val_start=today - timedelta(days=60),
        val_end=today - timedelta(days=30),
        test_start=today - timedelta(days=30),
        test_end=today - timedelta(days=1),
        label_window=LabelWindow(),
        min_history_days=60,
    )

    # 生成数据集
    logger.info("Generating Jev training dataset...")
    generator = JevDatasetGenerator(dataset_config)
    dataset_paths = generator.generate_dataset()

    # 训练模型
    model_config = JevModelConfig(
        d_model=256,
        nhead=8,
        num_encoder_layers=4,
        dim_feedforward=1024,
        dropout=0.1,
        max_seq_len=60,
        num_features=128,
        device="auto",
    )

    checkpoint_dir = config.jev_model_dir / f"auto_{today.isoformat()}"
    training_config = JevTrainingConfig(
        model_config=model_config,
        batch_size=256,
        learning_rate=1e-4,
        num_epochs=50,  # 自动训练使用较少 epoch
        checkpoint_dir=checkpoint_dir,
    )

    logger.info("Training Jev model...")
    trainer = JevTrainer(training_config)

    # TODO: 实际加载数据并训练
    # train_loader = _create_data_loader(dataset_paths["train"], training_config.batch_size)
    # val_loader = _create_data_loader(dataset_paths["val"], training_config.batch_size)
    # history = trainer.train(train_loader, val_loader)

    logger.warning("Jev auto training not yet fully implemented")

    return {
        "status": "success",
        "model_version": f"auto_{today.isoformat()}",
        "checkpoint_dir": str(checkpoint_dir),
        "dataset_paths": {k: str(v) for k, v in dataset_paths.items()},
    }


def _get_last_training_time(model_dir: Path) -> datetime | None:
    """获取最近一次训练时间"""
    if not model_dir.exists():
        return None

    latest = None
    for subdir in model_dir.iterdir():
        if subdir.is_dir() and subdir.name.startswith("auto_"):
            # 从目录名提取日期
            try:
                date_str = subdir.name.split("_", 1)[1]
                training_date = datetime.fromisoformat(date_str)
                if latest is None or training_date > latest:
                    latest = training_date
            except (ValueError, IndexError):
                continue

    return latest
