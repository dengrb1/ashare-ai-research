"""
API 端点：决策预测和模型管理。

提供 REST API 用于：
- POST /api/v1/decision/predict - 单个决策预测
- POST /api/v1/decision/batch - 批量决策预测
- GET /api/v1/decision/mode - 获取当前决策模式
- POST /api/v1/decision/mode - 切换决策模式
- GET /api/v1/decision/models - 列出可用模型
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, Field

from ashare_ai.agents.decision.models import UnifiedDecision
from ashare_ai.core.config import load_config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/decision", tags=["decision"])


class PredictRequest(BaseModel):
    """单个决策预测请求"""

    symbol: str = Field(..., pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    trading_date: date | None = None
    mode: Literal["legacy", "jev"] | None = None


class PredictResponse(BaseModel):
    """决策预测响应"""

    decision: UnifiedDecision
    status: str = "success"


class BatchPredictRequest(BaseModel):
    """批量决策预测请求"""

    symbols: list[str] = Field(..., min_length=1, max_length=100)
    trading_date: date | None = None
    mode: Literal["legacy", "jev"] | None = None


class BatchPredictResponse(BaseModel):
    """批量决策预测响应"""

    decisions: dict[str, UnifiedDecision]
    status: str = "success"
    failed_symbols: list[str] = Field(default_factory=list)


class ModeInfoResponse(BaseModel):
    """决策模式信息响应"""

    decision_mode: Literal["legacy", "jev"]
    fallback_enabled: bool
    jev_model_version: str | None = None
    jev_device: str | None = None


class ModeUpdateRequest(BaseModel):
    """决策模式更新请求"""

    mode: Literal["legacy", "jev"]
    fallback_enabled: bool | None = None


class ModelInfo(BaseModel):
    """模型信息"""

    version: str
    path: str
    mode: Literal["legacy", "jev"]


class ModelsListResponse(BaseModel):
    """模型列表响应"""

    models: list[ModelInfo]
    current_version: str
    status: str = "success"


@router.post("/predict", response_model=PredictResponse)
async def predict(
    request: Annotated[PredictRequest, Body(..., description="Prediction request")],
) -> PredictResponse:
    """
    生成单个股票的决策预测。

    Args:
        request: 包含股票代码、日期和模式的请求

    Returns:
        PredictResponse: 决策结果

    Raises:
        HTTPException: 预测失败时抛出
    """
    try:
        # TODO: 实际实现（需要集成 Bundle 加载和 orchestration）
        logger.warning("Decision predict endpoint not yet fully implemented")
        raise HTTPException(
            status_code=501,
            detail="Decision prediction not yet integrated with bundle loading",
        )

    except Exception as e:
        logger.error(f"Prediction failed for {request.symbol}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/batch", response_model=BatchPredictResponse)
async def batch_predict(
    request: Annotated[BatchPredictRequest, Body(..., description="Batch prediction request")],
) -> BatchPredictResponse:
    """
    批量生成决策预测。

    Args:
        request: 包含多个股票代码的请求

    Returns:
        BatchPredictResponse: 批量决策结果

    Raises:
        HTTPException: 预测失败时抛出
    """
    try:
        # TODO: 实际实现
        logger.warning("Batch prediction endpoint not yet fully implemented")
        raise HTTPException(
            status_code=501,
            detail="Batch prediction not yet integrated",
        )

    except Exception as e:
        logger.error(f"Batch prediction failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/mode", response_model=ModeInfoResponse)
async def get_mode() -> ModeInfoResponse:
    """
    获取当前决策模式配置。

    Returns:
        ModeInfoResponse: 当前决策模式信息
    """
    try:
        config = load_config()
        return ModeInfoResponse(
            decision_mode=config.decision_mode,
            fallback_enabled=config.decision_fallback_enabled,
            jev_model_version=config.jev_model_version if config.decision_mode == "jev" else None,
            jev_device=config.jev_device if config.decision_mode == "jev" else None,
        )

    except Exception as e:
        logger.error(f"Failed to get decision mode: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/mode", response_model=ModeInfoResponse)
async def update_mode(
    request: Annotated[ModeUpdateRequest, Body(..., description="Mode update request")],
) -> ModeInfoResponse:
    """
    更新决策模式。

    注意：此端点仅更新运行时配置，不持久化到 .env 文件。

    Args:
        request: 模式更新请求

    Returns:
        ModeInfoResponse: 更新后的模式信息

    Raises:
        HTTPException: 更新失败时抛出
    """
    try:
        # TODO: 实现运行时配置更新（需要全局配置管理器）
        logger.warning("Mode update endpoint not yet fully implemented")
        raise HTTPException(
            status_code=501,
            detail="Runtime mode switching not yet implemented",
        )

    except Exception as e:
        logger.error(f"Failed to update decision mode: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/models", response_model=ModelsListResponse)
async def list_models() -> ModelsListResponse:
    """
    列出可用的决策模型。

    Returns:
        ModelsListResponse: 可用模型列表

    Raises:
        HTTPException: 查询失败时抛出
    """
    try:
        config = load_config()
        models: list[ModelInfo] = []

        # Legacy 模型（始终可用）
        models.append(
            ModelInfo(
                version="legacy-v1.0.0",
                path="builtin",
                mode="legacy",
            )
        )

        # Jev 模型（从磁盘扫描）
        model_dir = config.jev_model_dir
        if model_dir.exists():
            for subdir in model_dir.iterdir():
                if subdir.is_dir() and (subdir / "config.json").exists():
                    models.append(
                        ModelInfo(
                            version=subdir.name,
                            path=str(subdir),
                            mode="jev",
                        )
                    )

        return ModelsListResponse(
            models=models,
            current_version=config.jev_model_version,
        )

    except Exception as e:
        logger.error(f"Failed to list models: {e}")
        raise HTTPException(status_code=500, detail=str(e))
