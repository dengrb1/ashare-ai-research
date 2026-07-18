import { useCallback, useEffect, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { api } from '../api'
import { CandlestickChart } from '../components/CandlestickChart'
import { Empty, ErrorNotice, formatNumber, formatTime, Loading, Panel, StatusPill, today } from '../components/Ui'
import { usePageRefresh } from '../context/RefreshContext'
import { getKlineRangePlan, KLINE_PERIODS, KLINE_RANGES, trimBarsToRange } from '../marketKlines'
import type { Candidate, KlineBar, KlineRange, MarketDataStatus, Quote, Report, Run, Score, TradePlan } from '../types'

function displayReportHtml(content: string) {
  return content
    .replace(/PLACEHOLDER_(\d+)/g, '行业数据暂缺（占位分组 $1）')
    .replace(
      '最大回撤熔断已触发：仅输出观察报告，不生成新增模拟仓位。',
      '当前为观察模式：数据完整性或回撤风控不满足正式组合要求，仅输出研究结果。',
    )
}

function values(value: unknown): string[] {
  if (Array.isArray(value)) return value.map((item) => typeof item === 'object' && item ? JSON.stringify(item) : String(item))
  return value === undefined || value === null || value === '' ? [] : [String(value)]
}

function explanationSection(explanation: Record<string, unknown>, keys: string[]) {
  for (const key of keys) {
    const result = values(explanation[key])
    if (result.length) return result
  }
  return []
}

const PLAN_ACTIVE = new Set(['PENDING', 'QUEUED', 'RUNNING', 'PROCESSING'])

export function ReportsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const [date, setDate] = useState(searchParams.get('date') || today())
  const runId = searchParams.get('run_id') || undefined
  const [report, setReport] = useState<Report | null>(null)
  const [content, setContent] = useState('')
  const [run, setRun] = useState<Run | null>(null)
  const [candidates, setCandidates] = useState<Candidate[]>([])
  const [symbol, setSymbol] = useState('')
  const [tradePlans, setTradePlans] = useState<TradePlan[]>([])
  const [score, setScore] = useState<Score | null>(null)
  const [lineage, setLineage] = useState<Record<string, unknown> | null>(null)
  const [quote, setQuote] = useState<Quote | null>(null)
  const [bars, setBars] = useState<KlineBar[]>([])
  const [marketStatus, setMarketStatus] = useState<MarketDataStatus | null>(null)
  const [period, setPeriod] = useState('day')
  const [range, setRange] = useState<KlineRange>('1m')
  const [loading, setLoading] = useState(true)
  const [marketLoading, setMarketLoading] = useState(false)
  const [planSubmitting, setPlanSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [marketError, setMarketError] = useState('')

  const load = useCallback(async () => {
    setLoading(true); setError(''); setContent('')
    try {
      const row = await api.report(date, runId)
      setReport(row)
      const [runsResult, candidatesResult, plansResult] = await Promise.allSettled([
        api.researchRuns(50, date), api.candidates(date, row.run_id), api.reportTradePlans(row.report_id),
      ])
      setRun(runsResult.status === 'fulfilled' ? runsResult.value.find((item) => item.run_id === row.run_id) || null : null)
      const formal = candidatesResult.status === 'fulfilled'
        ? candidatesResult.value.filter((item) => (item.event_risk_multiplier ?? 1) > 0).sort((left, right) => left.rank - right.rank) : []
      setCandidates(formal)
      setSymbol((current) => formal.some((item) => item.symbol === current) ? current : formal[0]?.symbol || '')
      setTradePlans(plansResult.status === 'fulfilled' ? plansResult.value : [])
      if (row.content || row.body) setContent(row.content || row.body || '')
      else {
        try { const detail = await api.reportContent(row.report_id); setContent(detail.content || detail.body || '') }
        catch { setContent('报告正文存储于对象存储，可通过审计记录核验内容哈希。') }
      }
    } catch (reason) {
      setReport(null); setRun(null); setCandidates([]); setTradePlans([])
      setError(reason instanceof Error ? reason.message : '报告加载失败')
    } finally { setLoading(false) }
  }, [date, runId])

  usePageRefresh(load)
  useEffect(() => { void load() }, [load])

  const rangePlan = useMemo(() => getKlineRangePlan(range), [range])
  useEffect(() => {
    if (!symbol || !report) return
    let alive = true
    setMarketLoading(true); setMarketError(''); setQuote(null); setBars([]); setMarketStatus(null); setScore(null); setLineage(null)
    void Promise.allSettled([
      api.quotes([symbol]),
      api.kline(symbol, period, 1200, { start: rangePlan.start, end: rangePlan.end }),
      api.score(date, symbol, report.run_id),
      api.scoreLineage(date, symbol, report.run_id),
    ]).then(([quoteResult, klineResult, scoreResult, lineageResult]) => {
      if (!alive) return
      if (quoteResult.status === 'fulfilled') setQuote(quoteResult.value[0] || null)
      if (klineResult.status === 'fulfilled') {
        setBars(trimBarsToRange(klineResult.value.bars, rangePlan))
        setMarketStatus(klineResult.value.status || null)
      }
      if (scoreResult.status === 'fulfilled') setScore(scoreResult.value)
      if (lineageResult.status === 'fulfilled') setLineage(lineageResult.value)
      const failed = [quoteResult, klineResult].filter((item) => item.status === 'rejected')
      if (failed.length) setMarketError('实时行情或 K 线加载失败；冻结评分与研究结论不受影响。')
    }).finally(() => { if (alive) setMarketLoading(false) })
    return () => { alive = false }
  }, [date, period, rangePlan, report, symbol])

  const selectedCandidate = candidates.find((item) => item.symbol === symbol)
  const selectedPlan = tradePlans.find((plan) => plan.symbols.length === 1
    && plan.symbols[0] === symbol
    && (plan.status.toUpperCase() === 'SUCCEEDED' || PLAN_ACTIVE.has(plan.status.toUpperCase())))
  const fused = run?.status.toUpperCase() === 'FUSED'
  const activePlanIds = tradePlans.filter((item) => PLAN_ACTIVE.has(item.status.toUpperCase())).map((item) => item.plan_id)
  useEffect(() => {
    if (!report || !activePlanIds.length) return
    const timer = window.setInterval(() => void api.reportTradePlans(report.report_id).then(setTradePlans).catch(() => undefined), 2500)
    return () => window.clearInterval(timer)
  }, [activePlanIds.join(','), report])

  async function submitPlan() {
    if (!report || !symbol || fused || selectedPlan) return
    setPlanSubmitting(true); setError('')
    try {
      const created = await api.submitTradePlan(report.report_id, { symbols: [symbol], objective: 'RISK_ADJUSTED_RETURN' })
      setTradePlans((current) => [created, ...current])
    } catch (reason) { setError(reason instanceof Error ? reason.message : '模拟交易方案提交失败') }
    finally { setPlanSubmitting(false) }
  }

  function changeDate(next: string) { setDate(next); setSearchParams({ date: next }) }
  const deterministic = selectedPlan?.deterministic_result as { outcome?: string; retained_cash?: number | string; symbol_plans?: Array<Record<string, unknown>>; conditions?: unknown } | null | undefined
  const rawExplanation = (selectedPlan?.ai_explanation || {}) as Record<string, unknown>
  const explanationItems = rawExplanation.items && typeof rawExplanation.items === 'object'
    ? rawExplanation.items as Record<string, unknown> : null
  const symbolExplanation = explanationItems?.[symbol]
  const explanation = (symbolExplanation && typeof symbolExplanation === 'object'
    ? symbolExplanation
    : rawExplanation.explanation && typeof rawExplanation.explanation === 'object'
      ? rawExplanation.explanation : rawExplanation) as Record<string, unknown>
  const explanationAvailable = (rawExplanation.status === 'SUCCEEDED' || rawExplanation.status === undefined)
    && Object.keys(explanation).some((key) => !['status', 'items', 'message'].includes(key))

  return <div className="report-layout">
    <Panel title="日报索引" eyebrow="RESEARCH REPORT" action={<label className="date-filter">交易日<input type="date" value={date} onChange={(event) => changeDate(event.target.value)} /></label>}>
      {report ? <div className="report-index"><div><span>报告类型</span><strong>{report.report_type}</strong></div><div><span>报告 ID</span><code>{report.report_id}</code></div><div><span>关联运行</span><code>{report.run_id}</code></div><div><span>生成时间</span><strong>{formatTime(report.created_at)}</strong></div></div> : !loading && <Empty title="暂无报告索引" />}
    </Panel>
    <Panel title={`${date} A 股每日研究报告`} eyebrow="DAILY BRIEF" className="report-paper">
      {fused && <div className="warning-box"><StatusPill status="FUSED" /><p>{run?.reason_message || '正式组合条件未满足'}。候选、行情、K 线与评分仅供观察。</p></div>}
      <ErrorNotice message={error} />
      {loading ? <Loading /> : report ? content ? <iframe className="report-frame" title={`${date} A 股每日研究报告正文`} sandbox="" srcDoc={displayReportHtml(content)} /> : <Empty title="报告正文暂不可用" /> : <Empty title="该交易日暂无报告" />}
    </Panel>

    {report && <Panel title="正式候选个股工作台" eyebrow="SINGLE-SYMBOL WORKBENCH" className="full-span">
      {candidates.length ? <div className="candidate-tabs" role="radiogroup" aria-label="正式候选">{candidates.map((candidate) => <button role="radio" aria-checked={symbol === candidate.symbol} className={symbol === candidate.symbol ? 'active' : ''} key={candidate.symbol} onClick={() => setSymbol(candidate.symbol)}><strong>#{candidate.rank} {candidate.symbol}</strong><small>最终分 {formatNumber(candidate.total_score)}</small></button>)}</div> : <Empty title="没有正式候选" description="被风险门禁排除的股票不会进入工作台" />}
      {symbol && <>
        <div className="workbench-grid">
          <section className="workbench-card"><span>最新行情</span><strong>{quote?.name || symbol} · {formatNumber(quote?.price)}</strong><small>{quote ? `${quote.change_pct >= 0 ? '+' : ''}${formatNumber(quote.change_pct)}%` : '行情暂不可用'}</small></section>
          <section className="workbench-card"><span>行情来源</span><strong>{quote?.source || marketStatus?.source || '—'}</strong><small>采集 {formatTime(quote?.collected_at || marketStatus?.collected_at)} · {(quote?.delayed ?? marketStatus?.delayed) ? '延迟数据' : '未标记延迟'}</small></section>
          <section className="workbench-card warning"><span>使用边界</span><strong>实时行情不参与研究结论</strong><small>结论仅来自冻结快照和确定性评分。</small></section>
        </div>
        <div className="kline-selectors"><div><span>采样周期</span><div className="period-tabs">{KLINE_PERIODS.map((item) => <button key={item.value} className={period === item.value ? 'active' : ''} onClick={() => setPeriod(item.value)}>{item.label}</button>)}</div></div><div><span>查看区间</span><div className="period-tabs">{KLINE_RANGES.map((item) => <button key={item.value} className={range === item.value ? 'active' : ''} onClick={() => setRange(item.value)}>{item.label}</button>)}</div></div></div>
        <ErrorNotice message={marketError} />
        {marketLoading && !bars.length ? <Loading label="加载行情与评分" /> : bars.length ? <CandlestickChart key={`${symbol}:${period}:${range}`} bars={bars} period={period} /> : <Empty title="K 线暂不可用" />}

        <h3>确定性评分</h3>
        {score ? <div className="score-grid">{[
          ['最终分', score.total_score], ['基础分', score.base_total_score], ['基本面', score.fundamental_score], ['技术', score.technical_score], ['情绪', score.sentiment_score], ['质量', score.quality_confidence_score], ['分红加分', score.dividend_bonus], ['风险乘数', score.event_risk_multiplier], ['预测分位', selectedCandidate?.prediction_percentile ?? score.prediction_percentile], ['排名', selectedCandidate?.rank ?? score.rank],
        ].map(([label, value]) => <div key={String(label)}><span>{label}</span><strong>{typeof value === 'number' ? formatNumber(value) : '—'}</strong></div>)}<div><span>公式版本</span><strong>{score.formula_version || String(lineage?.formula_version || '—')}</strong></div></div> : <Empty title="评分暂不可用" />}

        <div className="snapshot-callout"><span>◇</span><div><strong>确定性方案，AI 仅负责解释</strong><p>方案选择历史样本中风险调整后表现最优的合格参数；模型不可用时 BUY / NO_BUY、数量、限价、仓位和退出规则仍然有效。</p></div></div>
        {fused && <div className="warning-box"><strong>固定观察结论</strong><p>FUSED 运行禁止生成交易方案。</p></div>}
        <button className="primary" disabled={fused || planSubmitting || !symbol || Boolean(selectedPlan)} onClick={() => void submitPlan()}>{planSubmitting ? '正在生成…' : selectedPlan ? (PLAN_ACTIVE.has(selectedPlan.status.toUpperCase()) ? '方案生成中，已复用' : '已展示该股方案') : '生成总结与近期判断'}</button>

        {selectedPlan && <article className="research-run-card plan-workbench"><div className="research-run-head"><div><strong>{symbol}</strong><code>{selectedPlan.plan_id}</code></div><StatusPill status={selectedPlan.status} /></div>
          {selectedPlan.status.toUpperCase() === 'FAILED' && <div className="failure-box"><strong>方案生成失败</strong><p>{selectedPlan.error_message || '请查看审计事件'}</p></div>}
          {selectedPlan.status.toUpperCase() === 'SUCCEEDED' && deterministic && <><div className={deterministic.outcome === 'BUY' ? 'success-box' : 'warning-box'}><strong>{deterministic.outcome === 'BUY' ? 'BUY · 满足确定性买入条件' : 'NO_BUY · 当前不满足买入条件'}</strong><p>保留现金 {String(deterministic.retained_cash ?? '—')} 元；条件：{values(deterministic.conditions).join('；') || '详见逐项原因码与约束结果。'}</p></div>
            <div className="table-wrap"><table><thead><tr><th>动作与条件</th><th>限价有效期</th><th>仓位</th><th>止盈止损</th><th>样本外指标</th></tr></thead><tbody>{(deterministic.symbol_plans || []).map((item) => { const strategy = (item.strategy || {}) as Record<string, unknown>; const metrics = (strategy.validation_metrics || item.validation_metrics || {}) as Record<string, unknown>; return <tr key={String(item.symbol || symbol)}><td>{String(item.action ?? item.outcome ?? '—')}<small>{String(item.reason_code ?? item.reason ?? '—')}</small></td><td>{String(item.limit_price_low ?? '—')}–{String(item.limit_price_high ?? '—')}<small>{String(item.entry_valid_from ?? '—')} 至 {String(item.entry_valid_until ?? '—')}</small></td><td>{String(item.suggested_additional_quantity ?? 0)} 股<small>目标权重 {item.target_weight === undefined ? '—' : `${(Number(item.target_weight) * 100).toFixed(2)}%`}</small></td><td>止盈 {String(item.take_profit_price ?? '—')} · 止损 {String(item.stop_loss_price ?? '—')}<small>最长 {String(item.maximum_holding_sessions ?? '—')} 个交易日</small></td><td>净收益 {metrics.net_return === undefined ? '—' : `${(Number(metrics.net_return) * 100).toFixed(2)}%`}<small>Sharpe {metrics.sharpe === undefined ? '—' : Number(metrics.sharpe).toFixed(2)} · 回撤 {metrics.maximum_drawdown === undefined ? '—' : `${(Number(metrics.maximum_drawdown) * 100).toFixed(2)}%`}</small></td></tr> })}</tbody></table></div>
            {explanationAvailable ? <div className="ai-explanation"><h3>AI 解释</h3>{[['入场', ['entry_logic', 'entry', 'entry_summary', 'entry_conditions']], ['退出', ['exit_logic', 'exit', 'exit_summary', 'exit_conditions']], ['关键证据', ['key_evidence', 'evidence']], ['风险', ['risks', 'risk_warnings']]].map(([label, keys]) => <section key={String(label)}><strong>{String(label)}</strong><ul>{explanationSection(explanation, keys as string[]).map((item) => <li key={item}>{item}</li>)}</ul></section>)}</div> : <div className="warning-box"><strong>AI 解释不可用</strong><p>确定性结论与全部数值仍有效。</p></div>}</>}
        </article>}
      </>}
    </Panel>}
  </div>
}
