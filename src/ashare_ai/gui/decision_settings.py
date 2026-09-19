"""
GUI 系统设置页扩展 - 决策模式切换。

为系统设置页添加决策模式相关配置项：
- 决策模式选择（Legacy/Jev）
- Fallback 开关
- Jev 模型选择
- 设备选择（CPU/CUDA）
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel


class DecisionModeSettings(BaseModel):
    """决策模式设置（前端配置对象）"""

    decision_mode: str = "legacy"
    decision_fallback_enabled: bool = False
    jev_model_version: str = "jev-baseline-v1"
    jev_device: str = "auto"


class DecisionModeSettingsHandler:
    """决策模式设置处理器（后端 API）"""

    def __init__(self, config_path: Path | None = None):
        self.config_path = config_path or Path(".env")

    def get_current_settings(self) -> DecisionModeSettings:
        """获取当前设置"""
        # TODO: 从配置文件或环境变量读取
        from ashare_ai.core.config import load_config

        config = load_config()

        return DecisionModeSettings(
            decision_mode=config.decision_mode,
            decision_fallback_enabled=config.decision_fallback_enabled,
            jev_model_version=config.jev_model_version,
            jev_device=config.jev_device,
        )

    def update_settings(self, settings: DecisionModeSettings) -> None:
        """更新设置"""
        # TODO: 写入配置文件
        # TODO: 触发配置重载
        pass

    def list_available_models(self) -> list[dict[str, str]]:
        """列出可用模型"""
        from ashare_ai.core.config import load_config

        config = load_config()
        model_dir = config.jev_model_dir

        models = [{"version": "legacy-v1.0.0", "mode": "legacy", "path": "builtin"}]

        if model_dir.exists():
            for subdir in model_dir.iterdir():
                if subdir.is_dir() and (subdir / "config.json").exists():
                    models.append({
                        "version": subdir.name,
                        "mode": "jev",
                        "path": str(subdir),
                    })

        return models


# GUI 集成点（伪代码，实际需要根据前端框架实现）
"""
# React 组件示例（TypeScript）

interface DecisionModeSettings {
  decision_mode: "legacy" | "jev";
  decision_fallback_enabled: boolean;
  jev_model_version: string;
  jev_device: "auto" | "cpu" | "cuda";
}

function DecisionModeSettingsPanel() {
  const [settings, setSettings] = useState<DecisionModeSettings>({
    decision_mode: "legacy",
    decision_fallback_enabled: false,
    jev_model_version: "jev-baseline-v1",
    jev_device: "auto",
  });

  const handleSave = async () => {
    await fetch("/api/v1/decision/mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(settings),
    });
  };

  return (
    <div className="settings-panel">
      <h2>决策模式设置</h2>

      <div className="form-group">
        <label>决策模式</label>
        <select
          value={settings.decision_mode}
          onChange={(e) => setSettings({...settings, decision_mode: e.target.value})}
        >
          <option value="legacy">Legacy (规则评分)</option>
          <option value="jev">Jev (监督学习)</option>
        </select>
      </div>

      <div className="form-group">
        <label>
          <input
            type="checkbox"
            checked={settings.decision_fallback_enabled}
            onChange={(e) => setSettings({...settings, decision_fallback_enabled: e.target.checked})}
          />
          启用 Fallback（异常时回退到 Legacy）
        </label>
      </div>

      {settings.decision_mode === "jev" && (
        <>
          <div className="form-group">
            <label>Jev 模型版本</label>
            <select
              value={settings.jev_model_version}
              onChange={(e) => setSettings({...settings, jev_model_version: e.target.value})}
            >
              <option value="jev-baseline-v1">jev-baseline-v1</option>
              {/* 从 /api/v1/decision/models 加载更多选项 */}
            </select>
          </div>

          <div className="form-group">
            <label>计算设备</label>
            <select
              value={settings.jev_device}
              onChange={(e) => setSettings({...settings, jev_device: e.target.value})}
            >
              <option value="auto">自动选择</option>
              <option value="cpu">CPU</option>
              <option value="cuda">CUDA (GPU)</option>
            </select>
          </div>
        </>
      )}

      <button onClick={handleSave}>保存设置</button>
    </div>
  );
}
"""
