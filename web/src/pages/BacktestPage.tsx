import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { api, unwrapList } from '../api'
import { ErrorNotice, formatNumber, formatTime, Panel, StatusPill, today } from '../components/Ui'
import { usePollingTask } from '../hooks/usePollingTask'
import type { Snapshot } from '../types'

function isoOffset(days: number) { const value = new Date(); value.setDate(value.getDate() + days); return value.toISOString().slice(0, 10) }

export function BacktestPage() {
  const [form, setForm] = useState({ name: '核心候选组合回测', start: isoOffset(-90), end: today(), snapshots: '', benchmark: '000300.SH', capital: '1000000', fee: 'rule-driven' })
  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState('')
  const [snapshots, setSnapshots] = useState<Snapshot[]>([])
  const [statusBusy, setStatusBusy] = useState(false)
  const loadBacktest = useCallback((id: string) => api.backtest(id), [])
  const { task, error, watch, setTask } = usePollingTask(loadBacktest, 3000)

  useEffect(() => {
    api.snapshots().then(setSnapshots).catch((reason) => setSubmitError(reason instanceof Error ? reason.message : '快照列表加载失败'))
    api.backtests().then((payload) => {
      const latest = unwrapList(payload)[0]
      if (!latest) return
      setTask(latest)
      if (['PENDING', 'QUEUED', 'RUNNING', 'PROCESSING'].includes(latest.status.toUpperCase())) void watch(latest.backtest_id || latest.run_id)
    }).catch(() => undefined)
  }, [])

  function addSnapshot(snapshotId: string) {
    if (!snapshotId) return
    const selected = snapshots.find((snapshot) => snapshot.snapshot_id === snapshotId)
    const start = selected?.details?.calendar_start
    const end = selected?.details?.calendar_end
    setForm({
      ...form,
      snapshots: snapshotId,
      start: typeof start === 'string' ? start : form.start,
      end: typeof end === 'string' ? end : form.end,
    })
  }

  async function submit(event: FormEvent) {
    event.preventDefault(); setSubmitError('')
    const ids = form.snapshots.split(/[,\s]+/).map((item) => item.trim()).filter(Boolean)
    if (!ids.length) { setSubmitError('请至少输入一个已提交的快照 ID'); return }
    if (ids.length !== 1) { setSubmitError('请选择一个累计研究快照'); return }
    setSubmitting(true)
    try {
      const row = await api.submitBacktest({ name: form.name, start_date: form.start, end_date: form.end, snapshot_ids: ids, config: { benchmark: form.benchmark, initial_capital: Number(form.capital), fee_model: form.fee, adjustment: 'hfq' } })
      setTask(row); void watch(row.backtest_id || row.run_id)
    } catch (reason) { setSubmitError(reason instanceof Error ? reason.message : '回测提交失败') } finally { setSubmitting(false) }
  }

  const metrics = task?.metrics || {}
  const active = task && ['PENDING', 'QUEUED', 'RUNNING', 'PROCESSING'].includes(task.status.toUpperCase())
  const selectedSnapshot = snapshots.find((snapshot) => snapshot.snapshot_id === form.snapshots)
  const currentId = task?.backtest_id || task?.run_id

  async function refreshStatus() {
    if (!currentId || statusBusy) return
    setStatusBusy(true)
    try { setTask(await api.backtest(currentId)) }
    catch (reason) { setSubmitError(reason instanceof Error ? reason.message : '回测状态刷新失败') }
    finally { setStatusBusy(false) }
  }

  async function retry() {
    if (!currentId || statusBusy) return
    setStatusBusy(true); setSubmitError('')
    try {
      const next = await api.retryBacktest(currentId)
      setTask(next); void watch(currentId)
    } catch (reason) { setSubmitError(reason instanceof Error ? reason.message : '回测重新运行失败') }
    finally { setStatusBusy(false) }
  }
  return <div className="backtest-layout">
    <Panel title="回测配置" eyebrow="EVENT-DRIVEN BACKTEST">
      <form className="backtest-form" onSubmit={submit}>
        <label className="wide">回测名称<input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} required /></label>
        <label>开始日期<input type="date" value={form.start} onChange={(event) => setForm({ ...form, start: event.target.value })} required /></label><label>结束日期<input type="date" value={form.end} onChange={(event) => setForm({ ...form, end: event.target.value })} required /></label>
        <label>重点基准<select value={form.benchmark} onChange={(event) => setForm({ ...form, benchmark: event.target.value })}><option value="000300.SH">沪深 300</option><option value="000905.SH">中证 500</option><option value="EQUAL_WEIGHT_UNIVERSE">等权股票池</option></select></label>
        <label>初始资金<input type="number" min="10000" step="10000" value={form.capital} onChange={(event) => setForm({ ...form, capital: event.target.value })} /></label>
        <label className="wide">可用研究快照<select defaultValue="" onChange={(event) => { addSnapshot(event.target.value); event.target.value = '' }}><option value="">选择已提交快照…</option>{snapshots.map((snapshot) => <option key={snapshot.snapshot_id} value={snapshot.snapshot_id}>{snapshot.fetched_at.slice(0, 10)} · {snapshot.source} · {snapshot.row_count} 行{snapshot.details?.observe_only ? ' · 观察模式' : ''}</option>)}</select></label>
        <label className="wide">冻结快照 ID<textarea rows={2} placeholder={snapshots.length ? '从上方选择一个累计研究快照' : '暂无可用快照；先完成一次每日研究'} value={form.snapshots} onChange={(event) => setForm({ ...form, snapshots: event.target.value })} required /></label>
        {Boolean(selectedSnapshot?.details?.observe_only) && <div className="processing-note wide"><i />观察模式快照仅用于研究模拟，不代表可执行组合或实盘建议。</div>}
        <div className="backtest-rules wide"><strong>执行规则</strong><span>T+1</span><span>涨跌停</span><span>停复牌</span><span>申报单位</span><span>生效日费用</span><span>后复权序列</span></div>
        <ErrorNotice message={submitError} />
        <button className="primary large wide" disabled={submitting || Boolean(active)}>{submitting ? '正在提交…' : active ? '回测执行中' : '提交异步回测'}<span>→</span></button>
      </form>
    </Panel>
    <div className="backtest-result">
      <Panel title="任务状态" eyebrow="EXECUTION" action={<div className="task-actions">{task && <StatusPill status={task.status} />}<button className="secondary compact-action" onClick={() => void refreshStatus()} disabled={!currentId || statusBusy}>{statusBusy ? '刷新中' : '手动刷新'}</button></div>}>
        {task ? <div className="task-detail"><div className="task-id"><span>BACKTEST ID</span><code>{task.backtest_id || task.run_id}</code></div>{active && <div className="processing-note"><i />Redis Worker 正在处理固定快照</div>}<div className="task-meta"><div><span>开始日期</span><strong>{task.start_date || form.start}</strong></div><div><span>结束日期</span><strong>{task.end_date || form.end}</strong></div><div><span>完成时间</span><strong>{formatTime(task.completed_at || undefined)}</strong></div></div>{task.error_message && <div className="failure-box"><strong>回测失败</strong><p>{task.error_message}</p><button className="primary" onClick={() => void retry()} disabled={statusBusy}>{statusBusy ? '正在重新排队…' : '重新运行'}</button></div>}<ErrorNotice message={error} /></div> : <div className="run-await"><span>⟲</span><strong>等待回测任务</strong><p>提交后将持续轮询状态与结果。</p></div>}
      </Panel>
      <Panel title="绩效摘要" eyebrow="METRICS">
        {Object.keys(metrics).length ? <div className="metrics-board">{Object.entries(metrics).slice(0, 12).map(([key, value]) => <div key={key}><span>{key.replaceAll('_', ' ')}</span><strong>{typeof value === 'number' ? formatNumber(value, 4) : String(value)}</strong></div>)}</div> : <div className="metric-placeholder">回测完成后展示收益、回撤、风险和三基准对比指标</div>}
      </Panel>
    </div>
  </div>
}
