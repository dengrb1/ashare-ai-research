import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import type { Candidate, Run } from '../types'
import { Empty, ErrorNotice, formatNumber, Loading, Panel, StatusPill, today } from '../components/Ui'
import { usePageRefresh } from '../context/RefreshContext'

export function CandidatesPage() {
  const [date, setDate] = useState(today())
  const [rows, setRows] = useState<Candidate[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [run, setRun] = useState<Run | null>(null)
  const load = useCallback(async () => {
    setError('')
    try {
      const [candidates, recent] = await Promise.all([api.candidates(date), api.researchRuns(1, date)])
      setRows(candidates); setRun(recent[0] || null)
    } catch (reason) { setRows([]); setRun(null); setError(reason instanceof Error ? reason.message : '候选池加载失败') }
    finally { setLoading(false) }
  }, [date])
  usePageRefresh(load)
  useEffect(() => { setLoading(true); void load() }, [load])
  return <Panel title="研究候选池" eyebrow="DETERMINISTIC RANKING" action={<div className="filter-actions">{run && <StatusPill status={run.status} />}<label className="date-filter">交易日<input type="date" value={date} onChange={(event) => setDate(event.target.value)} /></label></div>}>
    {run?.status.toUpperCase() === 'FUSED' && <div className="warning-box"><strong>观察模式 · {run.reason_code || 'UNSPECIFIED'}</strong><p>{run.reason_message || '正式组合条件未满足'}；候选表仅包含通过数据门禁的股票，另有 {run.excluded_symbol_count || 0} 只保留为研究观察对象。</p></div>}
    <div className="table-note"><span>最终分由版本化公式生成</span><span>分红加分与事件风险乘数已纳入</span><span>不构成买卖建议</span></div>
    <ErrorNotice message={error} />
    {loading ? <Loading /> : rows.length ? <div className="table-wrap"><table><thead><tr><th>排名</th><th>证券</th><th>最终分</th><th>基础分</th><th>分红加分</th><th>预测分位</th><th>行业</th><th>事件风险</th><th>状态</th></tr></thead><tbody>{rows.map((row) => <tr key={row.symbol}><td><span className={`rank rank-${row.rank}`}>{String(row.rank).padStart(2, '0')}</span></td><td><strong>{row.symbol}</strong><small>{date}</small></td><td><div className="score-cell"><strong>{formatNumber(row.total_score)}</strong><i><span style={{ width: `${Math.min(100, row.total_score)}%` }} /></i></div></td><td>{formatNumber(row.base_total_score ?? row.total_score)}</td><td>+{formatNumber(row.dividend_bonus ?? 0)}</td><td>{formatNumber((row.prediction_percentile || 0) * (row.prediction_percentile && row.prediction_percentile <= 1 ? 100 : 1))}%</td><td>{row.industry_code || '—'}</td><td>{formatNumber((row.event_risk_multiplier ?? 1) * 100)}%</td><td><span className="status-pill success"><i />入选</span></td></tr>)}</tbody></table></div> : <Empty title="该交易日暂无候选结果" description="请先完成每日研究任务" />}
  </Panel>
}
