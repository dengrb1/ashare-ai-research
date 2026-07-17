import { useEffect, useState } from 'react'
import { api, unwrapList } from '../api'
import type { AuditEvent, Run } from '../types'
import { Empty, ErrorNotice, formatTime, Loading, Panel, StatusPill } from '../components/Ui'

export function RunsPage() {
  const [runs, setRuns] = useState<Run[]>([])
  const [selected, setSelected] = useState<Run | null>(null)
  const [audit, setAudit] = useState<AuditEvent[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  useEffect(() => { api.runs().then((payload) => { const rows = unwrapList(payload); setRuns(rows); setSelected(rows[0] || null) }).catch((reason) => setError(reason instanceof Error ? reason.message : '运行列表加载失败')).finally(() => setLoading(false)) }, [])
  useEffect(() => { if (!selected) return; setAudit([]); api.audit(selected.run_id).then(setAudit).catch(() => setAudit([])) }, [selected?.run_id])

  return <div className="runs-layout">
    <Panel title="运行记录" eyebrow="JOB RUNS" action={<span className="count-badge">{runs.length}</span>}>
      <ErrorNotice message={error} />
      {loading ? <Loading /> : runs.length ? <div className="run-list">{runs.map((run) => <button key={run.run_id} className={selected?.run_id === run.run_id ? 'active' : ''} onClick={() => setSelected(run)}><span className="run-type">{(run.run_type || 'RUN').replace('_', ' ')}</span><strong>{run.trading_date || run.name || '异步任务'}</strong><small>{run.run_id}</small><div><StatusPill status={run.status} /><time>{formatTime(run.started_at || run.created_at)}</time></div></button>)}</div> : <Empty title="暂无运行记录" />}
    </Panel>
    <div className="audit-column">
      <Panel title="运行详情" eyebrow="RUN DETAILS" action={selected && <StatusPill status={selected.status} />}>
        {selected ? <div className="detail-grid"><div><span>运行 ID</span><code>{selected.run_id}</code></div><div><span>类型</span><strong>{selected.run_type || '—'}</strong></div><div><span>交易日</span><strong>{selected.trading_date || '—'}</strong></div><div><span>开始</span><strong>{formatTime(selected.started_at || selected.created_at)}</strong></div><div><span>结束</span><strong>{formatTime(selected.completed_at || undefined)}</strong></div><div><span>归属</span><strong>当前用户</strong></div>{selected.error_message && <div className="failure-box wide"><strong>失败原因</strong><p>{selected.error_message}</p></div>}</div> : <Empty />}
      </Panel>
      <Panel title="审计时间线" eyebrow="AUDIT TRAIL">
        {audit.length ? <div className="timeline">{audit.map((event) => <div key={event.event_id} className={event.severity.toLowerCase()}><i /><div><div><strong>{event.event_type}</strong><time>{formatTime(event.created_at)}</time></div><p>{event.message}</p>{event.details && Object.keys(event.details).length > 0 && <details><summary>查看详情</summary><pre>{JSON.stringify(event.details, null, 2)}</pre></details>}</div></div>)}</div> : <Empty title="暂无审计事件" description="选择运行后读取可追溯事件" />}
      </Panel>
    </div>
  </div>
}
