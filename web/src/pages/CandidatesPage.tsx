import { useEffect, useState } from 'react'
import { api } from '../api'
import type { Candidate } from '../types'
import { Empty, ErrorNotice, formatNumber, Loading, Panel, today } from '../components/Ui'

export function CandidatesPage() {
  const [date, setDate] = useState(today())
  const [rows, setRows] = useState<Candidate[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  useEffect(() => { setLoading(true); setError(''); api.candidates(date).then(setRows).catch((reason) => { setRows([]); setError(reason instanceof Error ? reason.message : '候选池加载失败') }).finally(() => setLoading(false)) }, [date])
  return <Panel title="研究候选池" eyebrow="DETERMINISTIC RANKING" action={<label className="date-filter">交易日<input type="date" value={date} onChange={(event) => setDate(event.target.value)} /></label>}>
    <div className="table-note"><span>综合分由版本化公式生成</span><span>事件风险乘数已纳入</span><span>不构成买卖建议</span></div>
    <ErrorNotice message={error} />
    {loading ? <Loading /> : rows.length ? <div className="table-wrap"><table><thead><tr><th>排名</th><th>证券</th><th>综合分</th><th>预测分位</th><th>行业</th><th>事件风险</th><th>状态</th></tr></thead><tbody>{rows.map((row) => <tr key={row.symbol}><td><span className={`rank rank-${row.rank}`}>{String(row.rank).padStart(2, '0')}</span></td><td><strong>{row.symbol}</strong><small>{date}</small></td><td><div className="score-cell"><strong>{formatNumber(row.total_score)}</strong><i><span style={{ width: `${Math.min(100, row.total_score)}%` }} /></i></div></td><td>{formatNumber((row.prediction_percentile || 0) * (row.prediction_percentile && row.prediction_percentile <= 1 ? 100 : 1))}%</td><td>{row.industry_code || '—'}</td><td>{formatNumber((row.event_risk_multiplier ?? 1) * 100)}%</td><td><span className="status-pill success"><i />入选</span></td></tr>)}</tbody></table></div> : <Empty title="该交易日暂无候选结果" description="请先完成每日研究任务" />}
  </Panel>
}
