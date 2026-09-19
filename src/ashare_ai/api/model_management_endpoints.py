"""
API 端点扩展 - 模型管理。

扩展决策 API 端点，添加模型管理功能：
- 上传模型
- 删除模型
- 获取模型详情
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from ashare_ai.core.config import load_config
from ashare_ai.gui.model_management import ModelManager, ModelMetadata, ModelUploadRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/decision/models", tags=["decision-models"])


def _get_model_manager() -> ModelManager:
    """获取模型管理器实例"""
    config = load_config()
    return ModelManager(model_dir=config.jev_model_dir)


@router.get("/{version}", response_model=ModelMetadata)
async def get_model_details(version: str) -> ModelMetadata:
    """
    获取单个模型详情。

    Args:
        version: 模型版本

    Returns:
        ModelMetadata: 模型元数据

    Raises:
        HTTPException: 模型不存在时抛出
    """
    manager = _get_model_manager()
    metadata = manager.get_model(version)

    if metadata is None:
        raise HTTPException(status_code=404, detail=f"Model not found: {version}")

    return metadata


@router.post("/upload", response_model=ModelMetadata)
async def upload_model(
    file: Annotated[UploadFile, File(..., description="模型文件（zip 或目录）")],
    version: Annotated[str, Form(..., description="模型版本")],
    description: Annotated[str | None, Form(None, description="模型描述")] = None,
) -> ModelMetadata:
    """
    上传新模型。

    Args:
        file: 模型文件
        version: 模型版本
        description: 模型描述

    Returns:
        ModelMetadata: 上传后的模型元数据

    Raises:
        HTTPException: 上传失败时抛出
    """
    try:
        # TODO: 保存上传文件到临时目录
        # TODO: 解压（如果是 zip）
        # TODO: 验证模型格式
        # TODO: 调用 ModelManager.upload_model()

        logger.warning("Model upload endpoint not yet fully implemented")
        raise HTTPException(status_code=501, detail="Model upload not yet implemented")

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to upload model: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/{version}")
async def delete_model(version: str) -> dict[str, str]:
    """
    删除模型。

    Args:
        version: 模型版本

    Returns:
        dict: 成功消息

    Raises:
        HTTPException: 删除失败时抛出
    """
    try:
        manager = _get_model_manager()
        manager.delete_model(version)

        return {"status": "success", "message": f"Model deleted: {version}"}

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to delete model: {e}")
        raise HTTPException(status_code=500, detail=str(e))
