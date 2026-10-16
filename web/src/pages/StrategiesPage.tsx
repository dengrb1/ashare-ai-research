import { motion, useReducedMotion } from 'framer-motion'
import { BarChart3, FlaskConical, Play, RefreshCw, Sparkles } from 'lucide-react'
import { useEffect, useState } from 'react'
import { api, unwrapList } from '../api'
import { useMarket } from '../context/MarketContext'
import type { Candidate, Report, ResearchSubmission, Run, Snapshot } from '../types'
import { Empty, Loading, Panel, StatusPill, formatNumber, today } from '../components/Ui'

type Segment = 'pool' | 'backtest' | 'results'

export function StrategiesPage() {
  const reduceMotion = useReducedMotion()
  const { watchlist } = useMarket()
  const [segment, setSegment] = useState<Segment>('pool')
  const [date, setDate] = useState(today())
  const [candidates, setCandidates] = useState<Candidate[]>([])
  const [report, setReport] = useState<Report | null>(null)
  const [runs, setRuns] = useState<Run[]>([])
  const [snapshots, setSnapshots] = useState<Snapshot[]>([])
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [backtestName, setBacktestName] = useState('策略回测')
  const [trainingMode, setTrainingMode] = useState<'SNAPSHOT' | 'WATCHLIST_HISTORICAL'>('WATCHLIST_HISTORICAL')
  const [trainingSessions, setTrainingSessions] = useState('160')
  const [historySessions, setHistorySessions] = useState('240')
  const [assistText, setAssistText] = useState('')
  const [assistPreview, setAssistPreview] = useState<Record<string, unknown> | null>(null)
  const [assistWarnings, setAssistWarnings] = useState<string[]>([])
  const [researchAssistText, setResearchAssistText] = useState('')
  const [researchDraft, setResearchDraft] = useState<Partial<ResearchSubmission> | null>(null)

  async function load() {
    setLoading(true); setMessage(null)
    try {
      const [pool, latestReport, latestRuns, availableSnapshots] = await Promise.all([api.candidates(date), api.report(date).catch(() => null), api.runs(), api.snapshots()])
      setCandidates(pool); setReport(latestReport); setRuns(unwrapList(latestRuns)); setSnapshots(availableSnapshots)
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : '策略数据加载失败') } finally { setLoading(false) }
  }
  useEffect(() => { void load() }, [date])

  async function startResearch() {
    setBusy(true); setMessage(null)
    try {
      const draft = researchDraft || { trading_date: date, scope: watchlist.length ? 'WATCHLIST' : 'MARKET', symbols: watchlist }
      await api.submitResearch({ trading_date: String(draft.trading_date || date), scope: (draft.scope || 'MARKET') as ResearchSubmission['scope'], symbols: draft.symbols, total_budget: draft.total_budget, per_symbol_budget: draft.per_symbol_budget, max_stock_price: draft.max_stock_price, supreme_mode: draft.supreme_mode })
      setMessage('研究任务已进入 job-worker 队列'); setResearchDraft(null)
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : '研究提交失败') } finally { setBusy(false) }
  }
  async function startBacktest() {
    const snapshot = snapshots[0]
    if (trainingMode === 'SNAPSHOT' && !snapshot) { setMessage('当前没有可用回测快照'); return }
    if (trainingMode === 'WATCHLIST_HISTORICAL' && !watchlist.length) { setMessage('请先添加自选股'); return }
    setBusy(true)
    try {
      await api.submitBacktest({
        name: backtestName || '策略回测', start_date: date, end_date: date, mode: trainingMode,
        snapshot_ids: trainingMode === 'SNAPSHOT' ? [snapshot!.snapshot_id] : [],
        config: trainingMode === 'WATCHLIST_HISTORICAL'
          ? { scope: 'WATCHLIST', symbols: watchlist, training_sessions: Number(trainingSessions), history_sessions: Number(historySessions), entry_discounts: [0, 0.01, 0.02], use_real_watchlist_quotes: true, monitor_buy_entry_alerts: true }
          : {},
      })
      setMessage(trainingMode === 'WATCHLIST_HISTORICAL' ? '自选股历史数据采集任务已提交' : '回测任务已提交')
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : '回测提交失败') } finally { setBusy(false) }
  }

  async function runStrategyAssist() {
    if (!assistText.trim()) return
    try {
      const result = await api.assistForm({ form_type: 'STRATEGY', text: assistText, draft: { symbols: watchlist } })
      setAssistPreview(result.draft); setAssistWarnings(result.warnings)
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : 'AI 填报失败') }
  }

  async function runResearchAssist() {
    if (!researchAssistText.trim()) return
    try {
      const result = await api.assistForm({ form_type: 'RESEARCH', text: researchAssistText, draft: { trading_date: date, symbols: watchlist } })
      setResearchDraft(result.draft as Partial<ResearchSubmission>); setAssistWarnings(result.warnings)
    } catch (reason) { setMessage(reason instanceof Error ? reason.message : 'AI 研究参数解析失败') }
  }

  return <div className="page-stack strategies-page"><section className="strategies-head"><div><div className="eyebrow">RESEARCH WORKSPACE</div><h2>策略工作台</h2><p>选股池、回测与结构化研究结果共用一个标的复核区。</p></div><div className="strategy-actions"><label>研究日期<input type="date" value={date} onChange={(event) => setDate(event.target.value)} /></label><button className="secondary icon-text" onClick={() => void load()}><RefreshCw size={15} />刷新</button><button className="primary icon-text" disabled={busy} onClick={() => void startResearch()}><Play size={15} />运行研究</button></div></section>
    <div className="segmented-control" role="tablist" aria-label="策略视图"><button className={segment === 'pool' ? 'active' : ''} onClick={() => setSegment('pool')}><FlaskConical size={15} />选股池</button><button className={segment === 'backtest' ? 'active' : ''} onClick={() => setSegment('backtest')}><BarChart3 size={15} />回测</button><button className={segment === 'results' ? 'active' : ''} onClick={() => setSegment('results')}>研究结果</button></div>
    {message && <div className="snapshot-isolation">{message}</div>}
    {loading ? <Loading label="加载策略数据" /> : <motion.div key={segment} initial={reduceMotion ? false : { opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} className="strategy-layout">
      {segment === 'pool' && <><Panel title="候选标的" eyebrow="CANDIDATE POOL" action={<span className="metric-number">{candidates.length}</span>}><div className="candidate-table"><div className="table-header"><span>标的</span><span>总分</span><span>技术</span><span>风险</span></div>{candidates.length ? candidates.map((item) => <div className="candidate-row" key={item.symbol}><strong>{item.symbol}</strong><span>{formatNumber(item.total_score)}</span><span>{formatNumber(item.technical_score)}</span><StatusPill status={item.event_risk_multiplier && item.event_risk_multiplier < 1 ? 'WARNING' : 'ACTIVE'} /></div>) : <Empty title="暂无候选标的" description="运行一次研究任务后查看结构化评分" />}</div></Panel><Panel title="研究提交草稿" eyebrow="AI FORM ASSIST"><div className="ai-fill-row"><input value={researchAssistText} onChange={(event) => setResearchAssistText(event.target.value)} placeholder="例如：明天只研究自选，预算 10 万，单只不超过 5000" /><button className="secondary icon-text" onClick={() => void runResearchAssist()}><Sparkles size={15} />AI 生成草稿</button></div>{researchDraft && <div className="ai-preview"><strong>待确认研究参数</strong><pre>{JSON.stringify(researchDraft, null, 2)}</pre>{assistWarnings.map((warning) => <small key={warning}>{warning}</small>)}<button className="primary" disabled={busy} onClick={() => void startResearch()}>确认并提交研究</button></div>}</Panel><Panel title="统一复核" eyebrow="REVIEW ZONE"><div className="review-summary"><div><small>研究状态</small><StatusPill status={report ? 'SUCCEEDED' : 'PENDING'} /></div><div><small>组合结果</small><strong>{report ? '已生成' : '等待研究'}</strong></div><div><small>数据约束</small><strong>PIT</strong></div></div><pre className="structured-result">{report ? JSON.stringify(report.result || {}, null, 2) : '等待结构化研究结果'}</pre></Panel><Panel title="策略参数草稿" eyebrow="AI FORM ASSIST"><div className="ai-fill-row"><input value={assistText} onChange={(event) => setAssistText(event.target.value)} placeholder="描述策略目标、约束和参数" /><button className="secondary icon-text" onClick={() => void runStrategyAssist()}><Sparkles size={15} />AI 生成草稿</button></div>{assistPreview && <div className="ai-preview"><strong>待确认草稿</strong><pre>{JSON.stringify(assistPreview, null, 2)}</pre>{assistWarnings.map((warning) => <small key={warning}>{warning}</small>)}</div>}</Panel></>}
      {segment === 'backtest' && <><Panel title="回测任务" eyebrow="EVENT DRIVEN BACKTEST"><div className="run-form compact"><label>任务名称<input value={backtestName} onChange={(event) => setBacktestName(event.target.value)} /></label><label>训练来源<select value={trainingMode} onChange={(event) => setTrainingMode(event.target.value as typeof trainingMode)}><option value="WATCHLIST_HISTORICAL">自选股历史采集</option><option value="SNAPSHOT">已有冻结快照</option></select></label>{trainingMode === 'WATCHLIST_HISTORICAL' && <><label>训练交易日<input type="number" min="30" max="10000" value={trainingSessions} onChange={(event) => setTrainingSessions(event.target.value)} /></label><label>历史交易日<input type="number" min="30" max="2000" value={historySessions} onChange={(event) => setHistorySessions(event.target.value)} /></label></>}<button className="primary icon-text" disabled={busy} onClick={() => void startBacktest()}><Play size={15} />提交回测</button></div><div className="run-list">{runs.filter((run) => run.run_type === 'BACKTEST' || run.backtest_id).map((run) => <div className="run-row" key={run.run_id}><div><strong>{run.name || run.run_id}</strong><small>{run.created_at || run.trading_date}</small></div><StatusPill status={run.status} /></div>)}</div></Panel><Panel title="冻结快照" eyebrow="VALIDATION DATASET"><div className="snapshot-list">{snapshots.map((snapshot) => <div className="snapshot-row" key={snapshot.snapshot_id}><strong>{snapshot.dataset}</strong><span>{snapshot.row_count} 行</span><StatusPill status={snapshot.status} /></div>)}</div></Panel></>}
      {segment === 'results' && <><Panel title="结构化研究结果" eyebrow="REPORT CONTRACT"><div className="result-meta"><span>日期 <strong>{date}</strong></span><span>报告 <strong>{report?.report_type || '—'}</strong></span><span>版本 <strong>{report?.result ? 'v1' : '—'}</strong></span></div><pre className="structured-result large">{report ? JSON.stringify(report.result || {}, null, 2) : '当前日期没有结构化报告'}</pre></Panel><Panel title="运行记录" eyebrow="AUDIT TRAIL"><div className="run-list">{runs.slice(0, 12).map((run) => <div className="run-row" key={run.run_id}><div><strong>{run.run_type || 'RESEARCH'}</strong><small>{run.trading_date || run.created_at}</small></div><StatusPill status={run.status} /></div>)}</div></Panel></>}
    </motion.div>}
  </div>
}
