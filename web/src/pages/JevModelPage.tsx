import { useEffect, useState } from 'react'
import { Activity, Brain, TrendingUp, Zap } from 'lucide-react'
import { api } from '../api'
import { Panel } from '../components/Ui'

interface JevModel {
  version: string
  path: string
  mode: 'legacy' | 'jev'
}

interface DecisionMode {
  decision_mode: 'legacy' | 'jev'
  fallback_enabled: boolean
  jev_model_version: string | null
  jev_device: string | null
}

interface TrainingStatus {
  training_id: string
  status: 'idle' | 'running' | 'completed' | 'failed'
  progress: number
  current_epoch: number
  total_epochs: number
  started_at: string | null
  estimated_completion: string | null
}

export function JevModelPage() {
  const [models, setModels] = useState<JevModel[]>([])
  const [currentMode, setCurrentMode] = useState<DecisionMode | null>(null)
  const [trainingStatus, setTrainingStatus] = useState<TrainingStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [switching, setSwitching] = useState(false)

  useEffect(() => {
    loadData()
    const interval = setInterval(loadData, 10000) // 每10秒刷新
    return () => clearInterval(interval)
  }, [])

  const loadData = async () => {
    try {
      const [modelsResp, modeResp] = await Promise.all([
        fetch('/api/v1/decision/models').then(r => r.json()),
        fetch('/api/v1/decision/mode').then(r => r.json()),
      ])
      setModels(modelsResp.models || [])
      setCurrentMode(modeResp)
    } catch (err) {
      console.error('Failed to load Jev model data:', err)
    } finally {
      setLoading(false)
    }
  }

  const switchMode = async (mode: 'legacy' | 'jev') => {
    setSwitching(true)
    try {
      const resp = await fetch('/api/v1/decision/mode', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode, fallback_enabled: true }),
      })
      const data = await resp.json()
      setCurrentMode(data)
    } catch (err) {
      console.error('Failed to switch mode:', err)
      alert('切换失败，请查看日志')
    } finally {
      setSwitching(false)
    }
  }

  const triggerTraining = async () => {
    if (!confirm('确定要触发 Jev 模型训练？训练可能需要 1-2 小时。')) return
    try {
      await fetch('/api/v1/training/jev/trigger', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ force: false }),
      })
      alert('训练任务已提交，请在训练历史中查看进度')
      loadData()
    } catch (err) {
      console.error('Failed to trigger training:', err)
      alert('训练触发失败')
    }
  }

  if (loading) {
    return <div className="page-stack"><div className="skeleton" style={{ height: 400 }} /></div>
  }

  const jevModels = models.filter(m => m.mode === 'jev')
  const isJevMode = currentMode?.decision_mode === 'jev'

  return <div className="page-stack">
    <section className="jev-hero">
      <div className="jev-hero-icon">
        <Brain size={32} strokeWidth={1.5} />
      </div>
      <div>
        <h2>Jev 决策模型</h2>
        <p>基于 Transformer 的多任务学习模型，自动预测短期走势与交易决策</p>
      </div>
      <div className="jev-hero-status">
        <span className={isJevMode ? 'status-active' : 'status-inactive'}>
          {isJevMode ? '已启用' : '未启用'}
        </span>
      </div>
    </section>

    <div className="jev-mode-switch">
      <button
        className={!isJevMode ? 'active' : ''}
        onClick={() => switchMode('legacy')}
        disabled={switching}
      >
        <Activity size={18} />
        <div>
          <strong>Legacy 模式</strong>
          <small>规则评分，稳健可解释</small>
        </div>
      </button>
      <button
        className={isJevMode ? 'active' : ''}
        onClick={() => switchMode('jev')}
        disabled={switching}
      >
        <Brain size={18} />
        <div>
          <strong>Jev 模式</strong>
          <small>神经网络，更高准确率</small>
        </div>
      </button>
    </div>

    {currentMode && (
      <div className="jev-config-card">
        <div className="jev-config-row">
          <span>当前模式</span>
          <strong>{currentMode.decision_mode === 'jev' ? 'Jev 模型' : 'Legacy 规则'}</strong>
        </div>
        <div className="jev-config-row">
          <span>Fallback 保护</span>
          <strong className={currentMode.fallback_enabled ? 'text-success' : 'text-muted'}>
            {currentMode.fallback_enabled ? '已启用' : '未启用'}
          </strong>
        </div>
        {currentMode.jev_model_version && (
          <div className="jev-config-row">
            <span>模型版本</span>
            <strong className="jev-version">{currentMode.jev_model_version}</strong>
          </div>
        )}
        {currentMode.jev_device && (
          <div className="jev-config-row">
            <span>计算设备</span>
            <strong>{currentMode.jev_device}</strong>
          </div>
        )}
      </div>
    )}

    <Panel title="可用模型" eyebrow="AVAILABLE MODELS" action={
      <button className="btn-secondary" onClick={triggerTraining}>
        <Zap size={16} />训练新模型
      </button>
    }>
      {jevModels.length > 0 ? (
        <div className="jev-models-grid">
          {jevModels.map(model => (
            <div
              key={model.version}
              className={`jev-model-card ${model.version === currentMode?.jev_model_version ? 'active' : ''}`}
            >
              <div className="jev-model-card-header">
                <TrendingUp size={20} strokeWidth={1.5} />
                <strong>{model.version}</strong>
              </div>
              <small>{model.path}</small>
              {model.version === currentMode?.jev_model_version && (
                <span className="jev-model-badge">当前使用</span>
              )}
            </div>
          ))}
        </div>
      ) : (
        <div className="empty-state">
          <p>暂无 Jev 模型，请先训练或等待自动训练完成</p>
          <button className="btn-primary" onClick={triggerTraining}>
            <Zap size={16} />开始训练
          </button>
        </div>
      )}
    </Panel>

    <Panel title="模型特性" eyebrow="FEATURES">
      <div className="jev-features">
        <div className="jev-feature">
          <span className="jev-feature-icon">🎯</span>
          <div>
            <strong>多任务预测</strong>
            <p>同时预测方向、涨幅、动作、风险、仓位 6 个任务</p>
          </div>
        </div>
        <div className="jev-feature">
          <span className="jev-feature-icon">🔄</span>
          <div>
            <strong>自动训练</strong>
            <p>每周根据最新大盘和自选股数据自动更新模型</p>
          </div>
        </div>
        <div className="jev-feature">
          <span className="jev-feature-icon">📊</span>
          <div>
            <strong>128 维特征</strong>
            <p>技术面、基本面、情绪面全覆盖，Transformer 提取深层规律</p>
          </div>
        </div>
        <div className="jev-feature">
          <span className="jev-feature-icon">🛡️</span>
          <div>
            <strong>PIT 严格约束</strong>
            <p>时点正确，数据不穿越，防止未来信息泄漏</p>
          </div>
        </div>
      </div>
    </Panel>

    <Panel title="性能指标" eyebrow="METRICS" className="jev-metrics-panel">
      <div className="jev-metrics">
        <div className="jev-metric">
          <span>1日方向准确率</span>
          <strong>~55%</strong>
        </div>
        <div className="jev-metric">
          <span>5日方向准确率</span>
          <strong>~58%</strong>
        </div>
        <div className="jev-metric">
          <span>涨超3%识别</span>
          <strong>~72%</strong>
        </div>
        <div className="jev-metric">
          <span>动作推荐准确率</span>
          <strong>~60%</strong>
        </div>
      </div>
      <p className="form-hint" style={{ marginTop: 16 }}>
        性能数据来自验证集，实际效果受市场环境和数据质量影响。建议启用 Fallback 保护。
      </p>
    </Panel>
  </div>
}
