import { useCallback, useState } from 'react'
import { api } from '../api'
import { ErrorNotice, formatTime, Panel, StatusPill, today } from '../components/Ui'
import { usePollingTask } from '../hooks/usePollingTask'

export function ResearchPage() {
  const [date, setDate] = useState(today())
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState('')
  const loadRun = useCallback((id: string) => api.run(id), [])
  const { task, error, watch, setTask } = usePollingTask(loadRun)

  async function submit() {
    setSubmitting(true); setSubmitError('')
    try {
      const run = await api.submitResearch(date)
      setTask(run); void watch(run.run_id)
    } catch (reason) {
      setSubmitError(reason instanceof Error ? reason.message : '每日研究提交失败')
    } finally { setSubmitting(false) }
  }

  const status = task?.status.toUpperCase()
  const active = status && ['PENDING', 'QUEUED', 'RUNNING', 'PROCESSING'].includes(status)
  return <div className="research-layout">
    <Panel title="发起每日研究" eyebrow="NEW RESEARCH RUN">
      <div className="run-form">
        <label>交易日<input type="date" value={date} max={today()} onChange={(event) => setDate(event.target.value)} /></label>
        <div className="snapshot-callout"><span>▣</span><div><strong>冻结快照模式</strong><p>研究仅读取该交易日在决策时点可用的数据；实时行情不会写入本次快照。</p></div></div>
        <button className="primary large" onClick={() => void submit()} disabled={submitting || Boolean(active)}>{submitting ? '正在提交…' : active ? '任务执行中' : '启动每日研究'}<span>→</span></button>
        <ErrorNotice message={submitError} />
        <p className="form-hint">同一交易日重复提交会返回已有进行中任务，不会重复消耗上游数据与计算资源。</p>
      </div>
    </Panel>
    <Panel title="运行状态" eyebrow="LIVE PROGRESS" action={task && <StatusPill status={task.status} />}>
      {!task ? <div className="run-await"><span>✦</span><strong>等待研究任务</strong><p>选择交易日并启动后，将在此追踪 Redis 异步任务。</p></div> : <div className="task-detail">
        <div className="task-id"><span>RUN ID</span><code>{task.run_id}</code></div>
        <div className="progress-track"><i className={active ? 'animated' : status === 'FAILED' ? 'failed' : 'done'} /></div>
        <div className="task-meta"><div><span>交易日</span><strong>{task.trading_date || date}</strong></div><div><span>开始时间</span><strong>{formatTime(task.started_at || task.created_at)}</strong></div><div><span>完成时间</span><strong>{formatTime(task.completed_at || undefined)}</strong></div></div>
        {active && <div className="processing-note"><i />Worker 正在执行，页面每 2.5 秒同步状态</div>}
        {status === 'FAILED' && <div className="failure-box"><strong>运行失败</strong><p>{task.error_message || '未返回具体失败原因，请查看审计事件。'}</p></div>}
        {status && ['COMPLETED', 'SUCCESS', 'SUCCEEDED'].includes(status) && <div className="success-box"><strong>研究完成</strong><p>评分、候选池、模拟组合和日报已生成，可在对应页面查看。</p></div>}
        <ErrorNotice message={error} />
      </div>}
    </Panel>
    <Panel title="处理边界" eyebrow="DATA CONTRACT" className="full-span">
      <div className="contract-flow"><div><span>01</span><strong>available_at</strong><p>拒绝决策时点后的信息</p></div><i>→</i><div><span>02</span><strong>不可变快照</strong><p>Manifest 与内容哈希绑定</p></div><i>→</i><div><span>03</span><strong>Agent 结构化输出</strong><p>证据和子分严格校验</p></div><i>→</i><div><span>04</span><strong>确定性评分</strong><p>版本公式生成最终结果</p></div></div>
    </Panel>
  </div>
}
