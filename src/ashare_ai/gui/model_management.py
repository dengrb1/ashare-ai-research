"""
GUI 模型管理页面后端。

提供模型管理功能：
- 模型列表
- 模型上传
- 模型删除
- 模型信息查看
- 模型切换
"""

from __future__ import annotations

import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class ModelMetadata(BaseModel):
    """模型元数据"""

    version: str
    mode: str  # "legacy" | "jev"
    path: str
    created_at: datetime | None = None
    size_mb: float | None = None
    description: str | None = None
    metrics: dict[str, float] | None = None  # 训练指标


class ModelUploadRequest(BaseModel):
    """模型上传请求"""

    version: str
    description: str | None = None
    source_path: str  # 临时上传路径


class ModelManager:
    """模型管理器"""

    def __init__(self, model_dir: Path):
        self.model_dir = model_dir
        self.model_dir.mkdir(parents=True, exist_ok=True)

    def list_models(self) -> list[ModelMetadata]:
        """列出所有模型"""
        models: list[ModelMetadata] = []

        # Legacy 模型（内置）
        models.append(
            ModelMetadata(
                version="legacy-v1.0.0",
                mode="legacy",
                path="builtin",
                created_at=None,
                description="内置规则评分模型",
            )
        )

        # Jev 模型（从磁盘扫描）
        if self.model_dir.exists():
            for model_path in self.model_dir.iterdir():
                if model_path.is_dir():
                    try:
                        metadata = self._load_model_metadata(model_path)
                        models.append(metadata)
                    except Exception as e:
                        logger.warning(f"Failed to load model metadata from {model_path}: {e}")
                        continue

        return models

    def get_model(self, version: str) -> ModelMetadata | None:
        """获取单个模型信息"""
        if version == "legacy-v1.0.0":
            return ModelMetadata(
                version="legacy-v1.0.0",
                mode="legacy",
                path="builtin",
                description="内置规则评分模型",
            )

        model_path = self.model_dir / version
        if not model_path.exists():
            return None

        return self._load_model_metadata(model_path)

    def upload_model(self, request: ModelUploadRequest) -> ModelMetadata:
        """上传新模型"""
        target_path = self.model_dir / request.version

        if target_path.exists():
            raise ValueError(f"Model version already exists: {request.version}")

        # 复制模型文件
        source = Path(request.source_path)
        if not source.exists():
            raise FileNotFoundError(f"Source path not found: {source}")

        shutil.copytree(source, target_path)

        # 写入元数据
        metadata = ModelMetadata(
            version=request.version,
            mode="jev",
            path=str(target_path),
            created_at=datetime.now(),
            description=request.description,
        )

        self._save_model_metadata(target_path, metadata)

        logger.info(f"Model uploaded: {request.version}")
        return metadata

    def delete_model(self, version: str) -> None:
        """删除模型"""
        if version == "legacy-v1.0.0":
            raise ValueError("Cannot delete builtin legacy model")

        model_path = self.model_dir / version

        if not model_path.exists():
            raise FileNotFoundError(f"Model not found: {version}")

        shutil.rmtree(model_path)
        logger.info(f"Model deleted: {version}")

    def _load_model_metadata(self, model_path: Path) -> ModelMetadata:
        """从磁盘加载模型元数据"""
        metadata_file = model_path / "metadata.json"

        if metadata_file.exists():
            import json

            data = json.loads(metadata_file.read_text(encoding="utf-8"))
            return ModelMetadata(**data)

        # Fallback: 从 config.json 推断
        config_file = model_path / "config.json"
        if config_file.exists():
            stat = model_path.stat()
            return ModelMetadata(
                version=model_path.name,
                mode="jev",
                path=str(model_path),
                created_at=datetime.fromtimestamp(stat.st_ctime),
                size_mb=self._calculate_size_mb(model_path),
            )

        raise ValueError(f"No metadata or config found in {model_path}")

    def _save_model_metadata(self, model_path: Path, metadata: ModelMetadata) -> None:
        """保存模型元数据到磁盘"""
        metadata_file = model_path / "metadata.json"
        metadata_file.write_text(
            metadata.model_dump_json(indent=2, exclude_none=True),
            encoding="utf-8",
        )

    def _calculate_size_mb(self, model_path: Path) -> float:
        """计算模型目录大小"""
        total_size = sum(f.stat().st_size for f in model_path.rglob("*") if f.is_file())
        return total_size / (1024 * 1024)


# GUI 前端集成示例（伪代码）
"""
// React 组件示例

interface ModelMetadata {
  version: string;
  mode: "legacy" | "jev";
  path: string;
  created_at?: string;
  size_mb?: number;
  description?: string;
}

function ModelManagementPage() {
  const [models, setModels] = useState<ModelMetadata[]>([]);
  const [selectedModel, setSelectedModel] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/v1/decision/models")
      .then((res) => res.json())
      .then((data) => setModels(data.models));
  }, []);

  const handleDelete = async (version: string) => {
    if (!confirm(`确定删除模型 ${version}？`)) return;

    await fetch(`/api/v1/decision/models/${version}`, {
      method: "DELETE",
    });

    setModels(models.filter((m) => m.version !== version));
  };

  const handleUpload = async (file: File) => {
    const formData = new FormData();
    formData.append("file", file);
    formData.append("version", "jev-custom-v1");

    await fetch("/api/v1/decision/models/upload", {
      method: "POST",
      body: formData,
    });

    // 刷新列表
  };

  return (
    <div className="model-management">
      <h2>模型管理</h2>

      <div className="upload-section">
        <input type="file" onChange={(e) => handleUpload(e.target.files[0])} />
      </div>

      <table className="model-table">
        <thead>
          <tr>
            <th>版本</th>
            <th>模式</th>
            <th>大小</th>
            <th>创建时间</th>
            <th>描述</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {models.map((model) => (
            <tr key={model.version}>
              <td>{model.version}</td>
              <td>{model.mode}</td>
              <td>{model.size_mb?.toFixed(2)} MB</td>
              <td>{model.created_at || "-"}</td>
              <td>{model.description || "-"}</td>
              <td>
                {model.mode === "jev" && (
                  <button onClick={() => handleDelete(model.version)}>
                    删除
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
"""
