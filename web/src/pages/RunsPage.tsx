import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router'
import { api } from '../api'
import type { AuditEvent, RunActivity } from '../types'
import { Empty, ErrorNotice, formatTime, Loading, Panel, StatusPill } from '../components/Ui'

const TYPES = [
  ['', '全部'], ['DAILY,RESEARCH', '研究'], ['BACKTEST', '回测'],
  ['TRADE_PLAN', '买入方案'], ['EXIT_ADVICE', '卖出建议'],
] as const

export function RunsPage() {
  const [runs, setRuns] = useState<RunActivity[]>([])
  const [selected, setSelected] = useState<RunActivity | null>(null)
  const [audit, setAudit] = useState<AuditEvent[]>([])
  const [type, setType] = useState('')
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = useCallback(async (cursor?: string) => {
    setLoading(true); setError('')
    try {
      const payload = await api.runActivity({ cursor, type: type || undefined, limit: 30 })
      setRuns((current) => cursor ? [...current, ...payload.items] : payload.items)
      setSelected((current) => current && payload.items.some((item) => item.run_id === current.run_id) ? current : payload.items[0] || null)
      setNextCursor(payload.next_cursor || null)
    } catch (reason) { setError(reason instanceof Error ? reason.message : '运行列表加载失败') }
    finally { setLoading(false) }
  }, [type])

  useEffect(() => { void load() }, [load])
  useEffect(() => {
    if (!selected) return
    setAudit([])
    api.audit(selected.run_id).then(setAudit).catch(() => setAudit([]))
  }, [selected?.run_id])

  return <div className="runs-layout">
    <Panel title="运行记录" eyebrow="ACTIVITY" action={<span className="count-badge">{runs.length}</span>}>
      <div className="run-filters" role="group" aria-label="运行类型">{TYPES.map(([value, label]) => <button key={value} className={type === value ? 'active' : ''} onClick={() => setType(value)}>{label}</button>)}</div>
      <ErrorNotice message={error} />
      {loading && !runs.length ? <Loading /> : runs.length ? <div className="run-list">{runs.map((run) => <button key={run.run_id} className={selected?.run_id === run.run_id ? 'active' : ''} onClick={() => setSelected(run)}><span className="run-type">{run.title || (run.run_type || 'RUN').replace('_', ' ')}</span><strong>{run.symbol || run.trading_date || '异步任务'}</strong><small>{run.run_id}</small><div><StatusPill status={run.status} /><time>{formatTime(run.started_at || run.created_at)}</time></div></button>)}</div> : <Empty title="暂无运行记录" />}
      {nextCursor && <button className="secondary load-more" disabled={loading} onClick={() => void load(nextCursor)}>{loading ? '加载中…' : '加载更多'}</button>}
    </Panel>
    <div className="audit-column">
      <Panel title="运行详情" eyebrow="RUN DETAILS" action={selected && <StatusPill status={selected.status} />}>
        {selected ? <div className="detail-grid"><div><span>运行 ID</span><code>{selected.run_id}</code></div><div><span>类型</span><strong>{selected.title || selected.run_type || '—'}</strong></div><div><span>交易日</span><strong>{selected.trading_date || '—'}</strong></div><div><span>开始</span><strong>{formatTime(selected.started_at || selected.created_at)}</strong></div><div><span>结束</span><strong>{formatTime(selected.completed_at || undefined)}</strong></div><div><span>归属</span><strong>当前用户</strong></div>{selected.resource_url && <div className="wide"><span>关联资源</span><Link className="secondary resource-link" to={selected.resource_url}>打开{selected.title || '详情'}</Link></div>}{selected.error_message && <div className="failure-box wide"><strong>失败原因</strong><p>{selected.error_message}</p></div>}</div> : <Empty />}
      </Panel>
      <Panel title="审计时间线" eyebrow="AUDIT TRAIL">
        {audit.length ? <div className="timeline">{audit.map((event) => <div key={event.event_id} className={event.severity.toLowerCase()}><i /><div><div><strong>{event.event_type}</strong><time>{formatTime(event.created_at)}</time></div><p>{event.message}</p>{event.details && Object.keys(event.details).length > 0 && <details><summary>查看详情</summary><pre>{JSON.stringify(event.details, null, 2)}</pre></details>}</div></div>)}</div> : <Empty title="暂无审计事件" description="选择运行后读取可追溯事件" />}
      </Panel>
    </div>
  </div>
}
