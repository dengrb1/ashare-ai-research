"""
Jev 模型训练 CLI 命令。

提供命令行接口用于：
- 数据集生成
- 模型训练
- 模型评估
- Checkpoint 管理
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import typer

from ashare_ai.decision_training.dataset import JevDatasetConfig, JevDatasetGenerator, LabelWindow
from ashare_ai.decision_training.model import JevModelConfig, JevTrainer, JevTrainingConfig

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="train-jev",
    help="Jev 模型训练命令",
    no_args_is_help=True,
)


@app.command(name="generate-dataset")
def generate_dataset(
    bundle_dir: Path = typer.Option(
        ...,
        "--bundle-dir",
        help="CanonicalDailyBundle 存储目录",
    ),
    output_dir: Path = typer.Option(
        ...,
        "--output-dir",
        help="数据集输出目录",
    ),
    train_start: str = typer.Option(..., "--train-start", help="训练集起始日期 (YYYY-MM-DD)"),
    train_end: str = typer.Option(..., "--train-end", help="训练集结束日期"),
    val_start: str = typer.Option(..., "--val-start", help="验证集起始日期"),
    val_end: str = typer.Option(..., "--val-end", help="验证集结束日期"),
    test_start: str = typer.Option(..., "--test-start", help="测试集起始日期"),
    test_end: str = typer.Option(..., "--test-end", help="测试集结束日期"),
) -> None:
    """生成 Jev 模型训练数据集"""
    config = JevDatasetConfig(
        bundle_dir=bundle_dir,
        output_dir=output_dir,
        train_start=date.fromisoformat(train_start),
        train_end=date.fromisoformat(train_end),
        val_start=date.fromisoformat(val_start),
        val_end=date.fromisoformat(val_end),
        test_start=date.fromisoformat(test_start),
        test_end=date.fromisoformat(test_end),
    )

    generator = JevDatasetGenerator(config)
    paths = generator.generate_dataset()

    typer.echo(f"Dataset generated:")
    for split, path in paths.items():
        typer.echo(f"  {split}: {path}")


@app.command(name="train")
def train(
    dataset_dir: Path = typer.Option(..., "--dataset-dir", help="数据集目录"),
    checkpoint_dir: Path = typer.Option(..., "--checkpoint-dir", help="Checkpoint 输出目录"),
    d_model: int = typer.Option(256, "--d-model", help="模型维度"),
    nhead: int = typer.Option(8, "--nhead", help="注意力头数"),
    num_layers: int = typer.Option(4, "--num-layers", help="Encoder 层数"),
    batch_size: int = typer.Option(256, "--batch-size", help="批大小"),
    learning_rate: float = typer.Option(1e-4, "--lr", help="学习率"),
    num_epochs: int = typer.Option(100, "--epochs", help="训练轮数"),
    device: str = typer.Option("auto", "--device", help="设备 (auto/cpu/cuda)"),
) -> None:
    """训练 Jev 模型"""
    model_config = JevModelConfig(
        d_model=d_model,
        nhead=nhead,
        num_encoder_layers=num_layers,
        device=device,
    )

    training_config = JevTrainingConfig(
        model_config=model_config,
        batch_size=batch_size,
        learning_rate=learning_rate,
        num_epochs=num_epochs,
        checkpoint_dir=checkpoint_dir,
    )

    trainer = JevTrainer(training_config)

    # TODO: 加载数据集
    train_loader = None
    val_loader = None

    typer.echo("Training Jev model...")
    typer.echo(f"  Model: d_model={d_model}, nhead={nhead}, layers={num_layers}")
    typer.echo(f"  Training: batch_size={batch_size}, lr={learning_rate}, epochs={num_epochs}")

    history = trainer.train(train_loader, val_loader)

    typer.echo(f"Training completed. Final val_loss={history.get('val_loss', [])[-1] if history.get('val_loss') else 'N/A'}")


@app.command(name="evaluate")
def evaluate(
    checkpoint: Path = typer.Option(..., "--checkpoint", help="模型 checkpoint 路径"),
    dataset_dir: Path = typer.Option(..., "--dataset-dir", help="数据集目录"),
    split: str = typer.Option("test", "--split", help="数据集切分 (train/val/test)"),
) -> None:
    """评估 Jev 模型"""
    typer.echo(f"Evaluating checkpoint: {checkpoint}")
    typer.echo(f"Dataset: {dataset_dir}, split: {split}")

    # TODO: 加载模型和数据集
    # TODO: 运行评估
    # TODO: 输出详细指标

    typer.echo("Evaluation not yet implemented")


@app.command(name="list-checkpoints")
def list_checkpoints(
    checkpoint_dir: Path = typer.Option(..., "--checkpoint-dir", help="Checkpoint 目录"),
) -> None:
    """列出所有 checkpoints"""
    if not checkpoint_dir.exists():
        typer.echo(f"Checkpoint directory does not exist: {checkpoint_dir}")
        raise typer.Exit(1)

    checkpoints = sorted(checkpoint_dir.glob("*.pt"))

    if not checkpoints:
        typer.echo("No checkpoints found")
        return

    typer.echo(f"Found {len(checkpoints)} checkpoints:")
    for ckpt in checkpoints:
        typer.echo(f"  {ckpt.name}")


if __name__ == "__main__":
    app()
