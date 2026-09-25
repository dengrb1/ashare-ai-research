"""Optional Torch implementation of the Jev multi-task model.

Importing this module remains lightweight; PyTorch is loaded only when a model
is built or trained.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class JevModelConfig(BaseModel):
    d_model: int = 128
    nhead: int = 4
    num_encoder_layers: int = 2
    dim_feedforward: int = 256
    dropout: float = 0.1
    max_seq_len: int = 60
    num_features: int = 128
    device: str = "cpu"


class JevTransformer:
    HEADS = {"direction_1d": 3, "direction_5d": 3, "up_over_3pct_5d": 2, "action": 3, "risk": 3, "position": 7}

    def __init__(self, config: JevModelConfig):
        self.config = config
        self._model: Any | None = None

    def build_model(self) -> None:
        try:
            import torch
            from torch import nn
        except ImportError as exc:
            raise RuntimeError("PyTorch is not installed; install the jev extra to build a local model") from exc
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=self.config.d_model,
            nhead=self.config.nhead,
            dim_feedforward=self.config.dim_feedforward,
            dropout=self.config.dropout,
            batch_first=True,
            norm_first=True,
        )
        encoder = nn.TransformerEncoder(encoder_layer, num_layers=self.config.num_encoder_layers)
        self._model = nn.ModuleDict({
            "input": nn.Linear(self.config.num_features, self.config.d_model),
            "encoder": encoder,
            "heads": nn.ModuleDict({name: nn.Linear(self.config.d_model, size) for name, size in self.HEADS.items()}),
        })

    def forward(self, x: Any) -> dict[str, Any]:
        if self._model is None:
            self.build_model()
        pooled = self._model["encoder"](self._model["input"](x)).mean(dim=1)
        return {name: self._model["heads"][name](pooled) for name in self.HEADS}

    def save_checkpoint(self, path: Path, epoch: int, optimizer_state: dict | None = None) -> None:
        if self._model is None:
            raise RuntimeError("Model not built - call build_model() first")
        import torch

        path.parent.mkdir(parents=True, exist_ok=True)
        torch.save({
            "architecture_version": "jev-transformer-v1",
            "config": self.config.model_dump(),
            "state_dict": self._model.state_dict(),
            "optimizer_state": optimizer_state,
            "epoch": epoch,
            "metadata": {"feature_version": "v1.0.0"},
        }, path)

    def load_checkpoint(self, path: Path) -> dict[str, Any]:
        import torch

        payload = torch.load(path, map_location="cpu", weights_only=True)
        config = JevModelConfig.model_validate(payload.get("config", self.config.model_dump()))
        self.config = config
        self.build_model()
        self._model.load_state_dict(payload["state_dict"])
        return payload


class JevTrainingConfig(BaseModel):
    model: JevModelConfig = Field(alias="model_config")
    model_config = ConfigDict(populate_by_name=True)
    batch_size: int = 256
    learning_rate: float = 1e-4
    num_epochs: int = 100
    weight_decay: float = 1e-5
    checkpoint_dir: Path
    task_weights: dict[str, float] = Field(default_factory=lambda: {name: 1.0 for name in JevTransformer.HEADS})
    early_stopping_patience: int = 10


class JevTrainer:
    def __init__(self, config: JevTrainingConfig):
        self.config = config
        self.model = JevTransformer(config.model)
        self.config.checkpoint_dir.mkdir(parents=True, exist_ok=True)

    def compute_multi_task_loss(self, outputs: dict[str, Any], targets: dict[str, Any]) -> tuple[Any, dict[str, float]]:
        import torch.nn.functional as F

        losses = {name: F.cross_entropy(outputs[name], targets[name]) for name in self.model.HEADS if name in outputs and name in targets}
        total = sum(losses[name] * self.config.task_weights.get(name, 1.0) for name in losses)
        return total, {name: float(value.detach().cpu()) for name, value in losses.items()}

    def train(self, train_loader: Any, val_loader: Any) -> dict[str, Any]:
        import torch

        self.model.build_model()
        optimizer = torch.optim.AdamW(self.model._model.parameters(), lr=self.config.learning_rate, weight_decay=self.config.weight_decay)
        history: dict[str, Any] = {"train_loss": [], "val_loss": [], "val_accuracy": {}}
        for epoch in range(self.config.num_epochs):
            self.model._model.train()
            for features, targets in train_loader:
                optimizer.zero_grad(set_to_none=True)
                loss, _ = self.compute_multi_task_loss(self.model.forward(features), targets)
                loss.backward()
                torch.nn.utils.clip_grad_norm_(self.model._model.parameters(), 1.0)
                optimizer.step()
            metrics = self.evaluate(val_loader)
            history["val_loss"].append(metrics["val_loss"])
            self.model.save_checkpoint(self.config.checkpoint_dir / "checkpoint.pt", epoch, optimizer.state_dict())
        return history

    def evaluate(self, val_loader: Any) -> dict[str, float]:
        import torch

        self.model._model.eval()
        losses: list[float] = []
        correct: dict[str, int] = {name: 0 for name in self.model.HEADS}
        counts: dict[str, int] = {name: 0 for name in self.model.HEADS}
        with torch.no_grad():
            for features, targets in val_loader:
                outputs = self.model.forward(features)
                loss, _ = self.compute_multi_task_loss(outputs, targets)
                losses.append(float(loss.detach().cpu()))
                for name, logits in outputs.items():
                    correct[name] += int((logits.argmax(-1) == targets[name]).sum())
                    counts[name] += int(targets[name].numel())
        return {"val_loss": sum(losses) / max(1, len(losses)), **{f"{name}_acc": correct[name] / max(1, counts[name]) for name in self.model.HEADS}}
