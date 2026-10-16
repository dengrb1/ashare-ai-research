"""
API 端点：决策预测。

提供 REST API 用于：
- POST /api/v1/decision/predict - 单个决策预测
- POST /api/v1/decision/batch - 批量决策预测
- GET /api/v1/decision/mode - 获取当前决策模式
- POST /api/v1/decision/mode - 切换决策模式（由系统设置管理）
- GET /api/v1/decision/models - 列出可用决策公式

新预测只接受 ``factor`` 或兼容别名 ``legacy``；``jev`` 返回明确的弃用错误。
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time
from typing import Annotated, Literal

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, Field, field_validator

from ashare_ai.agents.decision.models import MarketState, UnifiedDecision
from ashare_ai.agents.decision.router import create_decision_router, normalize_request_mode
from ashare_ai.core.system_settings import get_effective_settings
from ashare_ai.orchestration.system2_jobs import get_system2_status
from ashare_ai.quant.factor_registry import load_active_factor_genome
from ashare_ai.scoring.factor import FACTOR_FORMULA_VERSION

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/decision", tags=["decision"])

# ``legacy`` is accepted as a compatibility alias and still yields a factor
# decision.  ``jev`` is deliberately absent so the request is rejected by
# validation before any provider runs.
RequestMode = Literal["factor", "legacy"]


def _validate_request_mode(value: str | None) -> str | None:
    if value is None:
        return None
    return normalize_request_mode(value)


class PredictRequest(BaseModel):
    """单个决策预测请求"""

    symbol: str = Field(..., pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    trading_date: date | None = None
    mode: str | None = None

    _mode = field_validator("mode")(_validate_request_mode)


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
    mode: str | None = None

    _mode = field_validator("mode")(_validate_request_mode)


class BatchPredictResponse(BaseModel):
    """批量决策预测响应"""

    decisions: dict[str, UnifiedDecision]
    status: str = "success"
    failed_symbols: list[str] = Field(default_factory=list)


class ModeInfoResponse(BaseModel):
    """决策模式信息响应"""

    decision_mode: Literal["factor", "legacy"]
    fallback_enabled: bool
    formula_version: str = FACTOR_FORMULA_VERSION
    confidence_threshold: float = Field(ge=0.0, le=1.0)
    system2_enabled: bool = True


class ModeUpdateRequest(BaseModel):
    """决策模式更新请求"""

    mode: str
    fallback_enabled: bool | None = None

    _mode = field_validator("mode")(_validate_request_mode)


class ModelInfo(BaseModel):
    """决策公式信息"""

    version: str
    path: str
    mode: Literal["factor"]


class ModelsListResponse(BaseModel):
    """公式列表响应"""

    models: list[ModelInfo]
    current_version: str
    status: str = "success"


def _build_router(mode: str | None):
    config = get_effective_settings()
    return create_decision_router(
        mode=mode or config.decision_mode,
        fallback_enabled=config.decision_fallback_enabled,
        genome=load_active_factor_genome(),
        confidence_threshold=config.factor_confidence_threshold,
        system2_enabled=config.decision_system2_enabled,
    )


@router.post("/predict", response_model=PredictResponse)
async def predict(
    request: Annotated[PredictRequest, Body(..., description="Prediction request")],
) -> PredictResponse:
    """
    生成单个股票的决策预测。

    Raises:
        HTTPException: 预测失败或模式已弃用时抛出
    """
    try:
        if not request.symbol or not request.symbol.endswith((".SH", ".SZ", ".BJ")):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid symbol format: {request.symbol}. Expected XXXXXX.(SH|SZ|BJ)",
            )

        trading_date = request.trading_date or date.today()
        decision_at = datetime.combine(trading_date, time(18, 0), tzinfo=UTC)
        if decision_at > datetime.now(UTC):
            raise HTTPException(status_code=422, detail="trading_date must not be in the future")
        # The public prediction endpoint accepts a frozen state payload only in
        # the research pipeline.  For a direct smoke prediction use a neutral,
        # fully PIT-valid state and let the deterministic factor formula decide.
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
        decision_router = _build_router(request.mode)
        decision = await decision_router.predict(state)
        confidence = decision_router._decision_confidence(decision)
        return PredictResponse(
            decision=decision,
            status="success",
            confidence=confidence,
            system2_state=decision_router.system2_state,
            system2_job_id=decision_router.system2_job_id,
        )

    except HTTPException:
        raise
    except ValueError as exc:
        # A retired mode is a client error, not a server failure.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as e:
        logger.error(f"Prediction failed for {request.symbol}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/batch", response_model=BatchPredictResponse)
async def batch_predict(
    request: Annotated[BatchPredictRequest, Body(..., description="Batch prediction request")],
) -> BatchPredictResponse:
    """批量生成决策预测。"""
    decisions: dict[str, UnifiedDecision] = {}
    failed: list[str] = []
    for symbol in request.symbols:
        try:
            result = await predict(
                PredictRequest(
                    symbol=symbol, trading_date=request.trading_date, mode=request.mode
                )
            )
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
    """获取当前决策模式配置。"""
    try:
        config = get_effective_settings()
        return ModeInfoResponse(
            decision_mode=normalize_request_mode(config.decision_mode),
            fallback_enabled=config.decision_fallback_enabled,
            confidence_threshold=config.factor_confidence_threshold,
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
    """
    try:
        normalize_request_mode(request.mode)
        raise HTTPException(status_code=409, detail="decision mode is managed by system settings")

    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as e:
        logger.error(f"Failed to update decision mode: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/models", response_model=ModelsListResponse)
async def list_models() -> ModelsListResponse:
    """
    列出可用的决策公式。

    只有版本化的确定性因子公式可用；不再扫描本地模型目录。
    """
    try:
        return ModelsListResponse(
            models=[
                ModelInfo(
                    version=FACTOR_FORMULA_VERSION,
                    path="builtin",
                    mode="factor",
                )
            ],
            current_version=FACTOR_FORMULA_VERSION,
        )

    except Exception as e:
        logger.error(f"Failed to list models: {e}")
        raise HTTPException(status_code=500, detail=str(e))
