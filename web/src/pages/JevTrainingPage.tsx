import { useEffect, useState } from 'react'
import { Activity, Brain, Play, Pause, TrendingUp, Zap, AlertCircle, CheckCircle } from 'lucide-react'
import { Panel, formatTime } from '../components/Ui'

interface TrainingStatus {
  training_id: string
  status: 'idle' | 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
  progress: number
  current_epoch: number
  total_epochs: number
  metrics: {
    train_loss?: number
    val_loss?: number
    val_accuracy?: Record<string, number>
  } | null
  started_at: string | null
  completed_at: string | null
  estimated_completion: string | null
  error: string | null
}

interface TrainingHistory {
  training_id: string
  status: 'completed' | 'failed' | 'cancelled'
  model_version: string
  started_at: string
  completed_at: string
  duration_minutes: number
  final_metrics: {
    val_loss: number
    val_accuracy: Record<string, number>
  }
}

export function JevTrainingPage() {
  const [currentTraining, setCurrentTraining] = useState<TrainingStatus | null>(null)
  const [history, setHistory] = useState<TrainingHistory[]>([])
  const [loading, setLoading] = useState(true)
  const [triggering, setTriggering] = useState(false)
  const [cancelling, setCancelling] = useState(false)

  useEffect(() => {
    loadTrainingStatus()
    const interval = setInterval(loadTrainingStatus, 5000) // 每5秒刷新
    return () => clearInterval(interval)
  }, [])

  const loadTrainingStatus = async () => {
    try {
      const [statusResp, historyResp] = await Promise.all([
        fetch('/api/v1/training/jev/status/current').then(r => r.json()).catch(() => null),
        fetch('/api/v1/training/jev/history?limit=10').then(r => r.json()).catch(() => ({ trainings: [] })),
      ])
      if (statusResp) setCurrentTraining(statusResp)
      if (historyResp.trainings) setHistory(historyResp.trainings)
    } catch (err) {
      console.error('Failed to load training status:', err)
    } finally {
      setLoading(false)
    }
  }

  const triggerTraining = async () => {
    if (!confirm('确定要启动 Jev 模型训练？\n\n预计耗时 1-2 小时，期间会使用系统资源。')) return

    setTriggering(true)
    try {
      const resp = await fetch('/api/v1/training/jev/trigger', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ force: false }),
      })
      const data = await resp.json()
      if (resp.ok) {
        alert(`✅ 训练任务已提交\n\nTask ID: ${data.training_id}`)
        await loadTrainingStatus()
      } else {
        alert(`❌ 训练触发失败: ${data.detail}`)
      }
    } catch (err) {
      console.error('Failed to trigger training:', err)
      alert('❌ 训练触发失败')
    } finally {
      setTriggering(false)
    }
  }

  const cancelTraining = async () => {
    if (!currentTraining || !confirm('确定要取消当前训练？')) return

    setCancelling(true)
    try {
      const resp = await fetch(`/api/v1/training/jev/cancel/${currentTraining.training_id}`, {
        method: 'POST',
      })
      if (resp.ok) {
        alert('✅ 训练已取消')
        await loadTrainingStatus()
      } else {
        alert('❌ 取消失败')
      }
    } catch (err) {
      console.error('Failed to cancel training:', err)
      alert('❌ 取消失败')
    } finally {
      setCancelling(false)
    }
  }

  const getStatusIcon = (status: string) => {
    switch (status) {
      case 'running':
      case 'queued':
        return <Activity size={20} className="text-accent animate-pulse" />
      case 'completed':
        return <CheckCircle size={20} className="text-down" />
      case 'failed':
      case 'cancelled':
        return <AlertCircle size={20} className="text-danger" />
      default:
        return <Brain size={20} className="text-muted" />
    }
  }

  const getStatusLabel = (status: string) => {
    const labels: Record<string, string> = {
      idle: '空闲',
      queued: '队列中',
      running: '训练中',
      completed: '已完成',
      failed: '失败',
      cancelled: '已取消',
    }
    return labels[status] || status
  }

  if (loading && !currentTraining) {
    return <div className="page-stack"><div className="skeleton" style={{ height: 400 }} /></div>
  }

  const isTraining = Boolean(currentTraining && ['queued', 'running'].includes(currentTraining.status))

  return <div className="page-stack">
    <section className="jev-training-hero">
      <div className="jev-training-hero-icon">
        <Brain size={40} strokeWidth={1.5} />
      </div>
      <div>
        <h2>Jev 模型训练中心</h2>
        <p>手动触发模型训练任务，监控训练进度，查看历史记录</p>
      </div>
    </section>

    {currentTraining && isTraining ? (
      <Panel title="当前训练任务" eyebrow="IN PROGRESS" className="jev-training-active">
        <div className="jev-training-progress">
          <div className="jev-progress-header">
            <div>
              <span>任务 ID</span>
              <strong className="jev-mono">{currentTraining.training_id}</strong>
            </div>
            <div className="jev-progress-status">
              <span>状态</span>
              <strong className="jev-status-running">{getStatusLabel(currentTraining.status)}</strong>
            </div>
          </div>

          <div className="jev-progress-bar">
            <div className="jev-progress-fill" style={{ width: `${currentTraining.progress * 100}%` }} />
          </div>
          <div className="jev-progress-text">
            <span>{Math.round(currentTraining.progress * 100)}% 完成</span>
            <span>
              Epoch {currentTraining.current_epoch}/{currentTraining.total_epochs}
            </span>
          </div>

          {currentTraining.metrics && (
            <div className="jev-metrics-inline">
              <div className="jev-metric-row">
                <span>训练损失</span>
                <strong>{currentTraining.metrics.train_loss?.toFixed(3) || '-'}</strong>
              </div>
              <div className="jev-metric-row">
                <span>验证损失</span>
                <strong>{currentTraining.metrics.val_loss?.toFixed(3) || '-'}</strong>
              </div>
              {currentTraining.metrics.val_accuracy?.action && (
                <div className="jev-metric-row">
                  <span>动作准确率</span>
                  <strong>{(currentTraining.metrics.val_accuracy.action * 100).toFixed(1)}%</strong>
                </div>
              )}
            </div>
          )}

          <div className="jev-progress-timing">
            <div>
              <span>开始时间</span>
              <strong>{currentTraining.started_at ? formatTime(currentTraining.started_at) : '-'}</strong>
            </div>
            {currentTraining.estimated_completion && (
              <div>
                <span>预计完成</span>
                <strong>{formatTime(currentTraining.estimated_completion)}</strong>
              </div>
            )}
          </div>

          <button
            className="btn-danger"
            onClick={cancelTraining}
            disabled={cancelling || currentTraining.status === 'completed'}
            style={{ marginTop: 12 }}
          >
            <Pause size={16} />
            {cancelling ? '取消中...' : '取消训练'}
          </button>
        </div>
      </Panel>
    ) : (
      <Panel title="启动新训练" eyebrow="MANUAL TRIGGER" action={
        <button
          className="btn-primary"
          onClick={triggerTraining}
          disabled={triggering || isTraining}
        >
          <Zap size={16} />
          {triggering ? '提交中...' : '启动训练'}
        </button>
      }>
        <div className="jev-training-description">
          <div className="jev-feature-item">
            <span>📊</span>
            <div>
              <strong>数据范围</strong>
              <p>使用过去 2 年历史数据 + 最新大盘走势，自动生成 128 维特征</p>
            </div>
          </div>
          <div className="jev-feature-item">
            <span>⚙️</span>
            <div>
              <strong>训练配置</strong>
              <p>Transformer 架构，4 层 encoder，8 头注意力，256 维模型</p>
            </div>
          </div>
          <div className="jev-feature-item">
            <span>🎯</span>
            <div>
              <strong>优化目标</strong>
              <p>多任务加权损失：方向(1.0) + 涨幅(1.0) + 动作(2.0) + 风险(1.5) + 仓位(1.5)</p>
            </div>
          </div>
          <div className="jev-feature-item">
            <span>⏱️</span>
            <div>
              <strong>预计耗时</strong>
              <p>1-2 小时（取决于数据量和系统性能）</p>
            </div>
          </div>
        </div>

        <div className="jev-training-warning">
          <AlertCircle size={16} />
          <div>
            <strong>注意事项</strong>
            <ul>
              <li>训练期间会占用 CPU/GPU 资源，可能影响其他功能</li>
              <li>模型训练完成后会自动保存，并更新版本号</li>
              <li>如果准确率相比现有模型有所提升，会自动切换为生产模型</li>
              <li>可随时点击"取消训练"中断任务</li>
            </ul>
          </div>
        </div>

        <button
          className="btn-primary"
          onClick={triggerTraining}
          disabled={triggering || isTraining}
          style={{ marginTop: 16, width: '100%' }}
        >
          <Play size={16} />
          {triggering ? '提交中...' : '立即启动训练'}
        </button>
      </Panel>
    )}

    {history.length > 0 && (
      <Panel title="训练历史" eyebrow="HISTORY" className="jev-history-panel">
        <div className="jev-history-table">
          <div className="jev-history-header">
            <div>任务 ID</div>
            <div>模型版本</div>
            <div>状态</div>
            <div>耗时</div>
            <div>验证损失</div>
            <div>动作准确率</div>
          </div>
          {history.map(record => (
            <div key={record.training_id} className="jev-history-row">
              <div className="jev-mono">{record.training_id}</div>
              <div>{record.model_version}</div>
              <div className={`jev-status-${record.status}`}>
                {getStatusIcon(record.status)}
                {getStatusLabel(record.status)}
              </div>
              <div>{record.duration_minutes} 分钟</div>
              <div>{record.final_metrics.val_loss.toFixed(3)}</div>
              <div>{(record.final_metrics.val_accuracy.action * 100).toFixed(1)}%</div>
            </div>
          ))}
        </div>
      </Panel>
    )}

    <Panel title="训练工作流" eyebrow="WORKFLOW">
      <div className="jev-workflow">
        <div className="jev-workflow-step">
          <div className="jev-workflow-badge">1</div>
          <div>
            <strong>数据准备</strong>
            <p>扫描 Bundle 数据，收集 2 年历史 + 最新行情</p>
          </div>
        </div>
        <div className="jev-workflow-step">
          <div className="jev-workflow-badge">2</div>
          <div>
            <strong>特征工程</strong>
            <p>提取 128 维特征（技术面、基本面、情绪面）</p>
          </div>
        </div>
        <div className="jev-workflow-step">
          <div className="jev-workflow-badge">3</div>
          <div>
            <strong>标签生成</strong>
            <p>生成 6 任务标签（方向、涨幅、动作、风险、仓位）</p>
          </div>
        </div>
        <div className="jev-workflow-step">
          <div className="jev-workflow-badge">4</div>
          <div>
            <strong>PIT 约束</strong>
            <p>应用时点正确性约束，防止未来信息泄漏</p>
          </div>
        </div>
        <div className="jev-workflow-step">
          <div className="jev-workflow-badge">5</div>
          <div>
            <strong>模型训练</strong>
            <p>Transformer 训练，AdamW 优化器，Cosine 调度器</p>
          </div>
        </div>
        <div className="jev-workflow-step">
          <div className="jev-workflow-badge">6</div>
          <div>
            <strong>模型评估</strong>
            <p>在验证集和测试集上评估性能指标</p>
          </div>
        </div>
        <div className="jev-workflow-step">
          <div className="jev-workflow-badge">7</div>
          <div>
            <strong>版本管理</strong>
            <p>保存 checkpoint，创建新版本号 (auto_YYYY-MM-DD)</p>
          </div>
        </div>
        <div className="jev-workflow-step">
          <div className="jev-workflow-badge">8</div>
          <div>
            <strong>自动切换</strong>
            <p>若性能更优，自动作为生产模型使用</p>
          </div>
        </div>
      </div>
    </Panel>
  </div>
}
