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
from datetime import UTC, date, datetime, time
from typing import Annotated, Literal

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, Field

from ashare_ai.agents.decision.models import MarketState, UnifiedDecision
from ashare_ai.agents.decision.router import create_decision_router
from ashare_ai.core.system_settings import get_effective_settings
from ashare_ai.orchestration.system2_jobs import get_system2_status

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
    confidence: float = Field(ge=0.0, le=1.0)
    system2_state: str = "IDLE"
    system2_job_id: str | None = None


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
    jev_backend: Literal["local", "live"] = "local"
    confidence_threshold: float = Field(ge=0.0, le=1.0)
    system2_enabled: bool = True


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
        # 验证股票代码格式
        if not request.symbol or not request.symbol.endswith(('.SH', '.SZ', '.BJ')):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid symbol format: {request.symbol}. Expected XXXXXX.(SH|SZ|BJ)"
            )

        config = get_effective_settings()
        trading_date = request.trading_date or date.today()
        decision_at = datetime.combine(trading_date, time(18, 0), tzinfo=UTC)
        if decision_at > datetime.now(UTC):
            raise HTTPException(status_code=422, detail="trading_date must not be in the future")
        # The public prediction endpoint accepts a frozen state payload only in
        # the research pipeline.  For a direct smoke prediction use a neutral,
        # fully PIT-valid state and let the configured provider decide.
        state = MarketState(
            symbol=request.symbol,
            trading_date=trading_date,
            available_at=decision_at,
            open=1.0,
            high=1.0,
            low=1.0,
            close=1.0,
            volume=0.0,
            amount=0.0,
        )
        router = create_decision_router(
            mode=request.mode or config.decision_mode,
            fallback_enabled=config.decision_fallback_enabled,
            jev_model_dir=config.jev_model_dir,
            jev_model_version=config.jev_model_version,
            jev_checkpoint=config.jev_checkpoint,
            jev_device=config.jev_device,
            jev_backend=config.jev_backend,
            jev_live_base_url=config.jev_live_base_url,
            jev_live_api_key=config.jev_live_api_key,
            jev_live_model=config.jev_live_model,
            jev_live_endpoint=config.jev_live_endpoint,
            jev_live_timeout_seconds=config.jev_live_timeout_seconds,
            confidence_threshold=config.jev_confidence_threshold,
            system2_enabled=config.decision_system2_enabled,
        )
        decision = await router.predict(state)
        confidence = router._decision_confidence(decision)
        return PredictResponse(
            decision=decision,
            status="success",
            confidence=confidence,
            system2_state=router.system2_state,
            system2_job_id=router.system2_job_id,
        )

    except HTTPException:
        raise
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
    decisions: dict[str, UnifiedDecision] = {}
    failed: list[str] = []
    for symbol in request.symbols:
        try:
            result = await predict(PredictRequest(symbol=symbol, trading_date=request.trading_date, mode=request.mode))
            decisions[symbol] = result.decision
        except HTTPException:
            failed.append(symbol)
    return BatchPredictResponse(decisions=decisions, failed_symbols=failed)


@router.get("/system2/{job_id}")
def system2_status(job_id: str) -> dict[str, str]:
    """Return the short-lived status of an asynchronously queued diagnostic."""

    import redis

    status = get_system2_status(
        redis.Redis.from_url(get_effective_settings().redis_url, decode_responses=True),
        job_id,
    )
    if status is None:
        raise HTTPException(status_code=404, detail="system2 diagnostic not found")
    return status


@router.get("/mode", response_model=ModeInfoResponse)
async def get_mode() -> ModeInfoResponse:
    """
    获取当前决策模式配置。

    Returns:
        ModeInfoResponse: 当前决策模式信息
    """
    try:
        config = get_effective_settings()
        return ModeInfoResponse(
            decision_mode=config.decision_mode,
            fallback_enabled=config.decision_fallback_enabled,
            jev_model_version=config.jev_model_version if config.decision_mode == "jev" else None,
            jev_device=config.jev_device if config.decision_mode == "jev" else None,
            jev_backend=config.jev_backend if config.decision_mode == "jev" else "local",
            confidence_threshold=config.jev_confidence_threshold,
            system2_enabled=config.decision_system2_enabled,
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
        raise HTTPException(status_code=409, detail="decision mode is managed by system settings")

    except HTTPException:
        raise
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
        config = get_effective_settings()
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
