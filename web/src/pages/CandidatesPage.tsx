import { useCallback, useEffect, useState } from 'react'
import { Link } from 'react-router'
import { api } from '../api'
import type { Candidate, Report, ReportSymbol, Run, TradePlan } from '../types'
import { Empty, ErrorNotice, formatNumber, Loading, Panel, StatusPill, today } from '../components/Ui'
import { usePageRefresh } from '../context/RefreshContext'
import { resolvePublishedResearchRun } from '../researchRuns'

const PLAN_ACTIVE = new Set(['PENDING', 'QUEUED', 'RUNNING', 'PROCESSING'])

export function CandidatesPage() {
  const [date, setDate] = useState(today())
  const [requestedDate, setRequestedDate] = useState<string | undefined>()
  const [rows, setRows] = useState<Candidate[]>([])
  const [report, setReport] = useState<Report | null>(null)
  const [reportSymbols, setReportSymbols] = useState<ReportSymbol[]>([])
  const [tradePlans, setTradePlans] = useState<TradePlan[]>([])
  const [submittingSymbol, setSubmittingSymbol] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [run, setRun] = useState<Run | null>(null)

  const load = useCallback(async () => {
    setError('')
    try {
      const selected = await resolvePublishedResearchRun(requestedDate)
      setRun(selected)
      if (!selected?.trading_date) {
        setRows([]); setReport(null); setReportSymbols([]); setTradePlans([])
        return
      }
      setDate(selected.trading_date)
      const [candidates, loadedReport] = await Promise.all([
        api.candidates(selected.trading_date, selected.run_id),
        api.report(selected.trading_date, selected.run_id),
      ])
      setRows(candidates); setReport(loadedReport)
      const [symbols, plans] = await Promise.all([
        api.reportSymbols(loadedReport.report_id),
        api.reportTradePlans(loadedReport.report_id),
      ])
      setReportSymbols(symbols); setTradePlans(plans)
    } catch (reason) {
      setRows([]); setReport(null); setReportSymbols([]); setTradePlans([]); setRun(null)
      setError(reason instanceof Error ? reason.message : '候选池加载失败')
    } finally { setLoading(false) }
  }, [requestedDate])

  usePageRefresh(load)
  useEffect(() => { setLoading(true); void load() }, [load])

  const activePlanIds = tradePlans.filter((item) => PLAN_ACTIVE.has(item.status.toUpperCase())).map((item) => item.plan_id)
  useEffect(() => {
    if (!report || !activePlanIds.length) return
    const timer = window.setInterval(() => {
      void api.reportTradePlans(report.report_id).then(setTradePlans).catch(() => undefined)
    }, 2500)
    return () => window.clearInterval(timer)
  }, [activePlanIds.join(','), report])

  async function submitPlan(symbol: string) {
    if (!report || run?.status.toUpperCase() === 'FUSED') return
    setSubmittingSymbol(symbol); setError('')
    try {
      const created = await api.submitTradePlan(report.report_id, {
        symbols: [symbol], objective: 'RISK_ADJUSTED_RETURN',
      })
      setTradePlans((current) => [created, ...current.filter((item) => item.plan_id !== created.plan_id)])
    } catch (reason) { setError(reason instanceof Error ? reason.message : '模拟买入建议提交失败') }
    finally { setSubmittingSymbol(null) }
  }

  function actionFor(row: Candidate) {
    const research = reportSymbols.find((item) => item.symbol === row.symbol)
    const plan = tradePlans.find((item) => item.symbols.includes(row.symbol))
    if (plan) {
      if (PLAN_ACTIVE.has(plan.status.toUpperCase())) return <span>方案生成中</span>
      return <Link className="secondary candidate-plan-link" to={`/reports?date=${encodeURIComponent(date)}&run_id=${encodeURIComponent(run?.run_id || '')}&symbol=${encodeURIComponent(row.symbol)}`}>查看方案</Link>
    }
    if (run?.status.toUpperCase() !== 'FUSED' && research?.advice_eligible) {
      return <button className="secondary" disabled={submittingSymbol === row.symbol} onClick={() => void submitPlan(row.symbol)}>{submittingSymbol === row.symbol ? '提交中…' : '生成买入建议'}</button>
    }
    return <span>—</span>
  }

  return <Panel title="研究候选池" eyebrow="DETERMINISTIC RANKING" action={<div className="filter-actions">{run && <StatusPill status={run.status} />}<label className="date-filter">交易日<input type="date" value={date} onChange={(event) => { setDate(event.target.value); setRequestedDate(event.target.value) }} /></label></div>}>
    {run?.status.toUpperCase() === 'FUSED' && <div className="warning-box"><strong>观察模式 · {run.reason_code || 'UNSPECIFIED'}</strong><p>{run.reason_message || '正式组合条件未满足'}；候选表仅包含通过数据门禁的股票，另有 {run.excluded_symbol_count || 0} 只保留为研究观察对象。</p></div>}
    <div className="table-note"><span>最终分由版本化公式生成</span><span>分红加分与事件风险乘数已纳入</span><span>不构成买卖建议</span></div>
    <ErrorNotice message={error} />
    {loading ? <Loading /> : rows.length ? <div className="table-wrap"><table><thead><tr><th>排名</th><th>证券</th><th>最终分</th><th>基础分</th><th>分红加分</th><th>预测分位</th><th>行业</th><th>事件风险</th><th>状态</th><th>操作</th></tr></thead><tbody>{rows.map((row) => <tr key={row.symbol}><td><span className={`rank rank-${row.rank}`}>{String(row.rank).padStart(2, '0')}</span></td><td><Link className="candidate-symbol-link" to={`/reports?date=${encodeURIComponent(date)}&run_id=${encodeURIComponent(run?.run_id || '')}&symbol=${encodeURIComponent(row.symbol)}`}><strong>{row.name || row.symbol}</strong><small>{row.symbol} · {date}</small></Link></td><td><div className="score-cell"><strong>{formatNumber(row.total_score)}</strong><i><span style={{ width: `${Math.min(100, row.total_score)}%` }} /></i></div></td><td>{formatNumber(row.base_total_score ?? row.total_score)}</td><td>+{formatNumber(row.dividend_bonus ?? 0)}</td><td>{formatNumber((row.prediction_percentile || 0) * (row.prediction_percentile && row.prediction_percentile <= 1 ? 100 : 1))}%</td><td>{row.industry_name || (row.industry_code?.startsWith('PLACEHOLDER_') ? '行业数据暂缺' : row.industry_code) || '—'}</td><td>{formatNumber((row.event_risk_multiplier ?? 1) * 100)}%</td><td><span className="status-pill success"><i />入选</span></td><td>{actionFor(row)}</td></tr>)}</tbody></table></div> : <Empty title="该交易日暂无候选结果" description="请先完成每日研究任务" />}
  </Panel>
}
