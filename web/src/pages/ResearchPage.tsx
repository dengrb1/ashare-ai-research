import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'
import { ErrorNotice, formatNumber, formatTime, Loading, Panel, StatusPill, today } from '../components/Ui'
import { useMarket } from '../context/MarketContext'
import { usePageRefresh } from '../context/RefreshContext'
import type { ResearchScope, ResearchSettings, Run } from '../types'

const ACTIVE = new Set(['PENDING', 'QUEUED', 'RUNNING', 'PROCESSING', 'CANCEL_REQUESTED'])

function normalizeSymbol(value: string) {
  const upper = value.trim().toUpperCase()
  if (/^\d{6}\.(SH|SZ|BJ)$/.test(upper)) return upper
  if (!/^\d{6}$/.test(upper)) return ''
  if (/^[48]/.test(upper)) return `${upper}.BJ`
  return /^[569]/.test(upper) ? `${upper}.SH` : `${upper}.SZ`
}

function parseSymbols(value: string) {
  return Array.from(new Set(value.split(/[\s,，;；]+/).map(normalizeSymbol).filter(Boolean)))
}

function positiveNumber(value: string) {
  if (!value.trim()) return undefined
  const parsed = Number(value)
  return Number.isFinite(parsed) && parsed > 0 ? parsed : undefined
}

const SCOPE_LABELS: Record<ResearchScope, string> = {
  MARKET: '动态市场股票池',
  WATCHLIST: '我的自选与持仓',
  CUSTOM: '手工指定股票',
}

export function ResearchPage() {
  const { watchlist, positions, quotes, subscribe } = useMarket()
  const [date, setDate] = useState(today())
  const [scope, setScope] = useState<ResearchScope>('MARKET')
  const [customSymbols, setCustomSymbols] = useState('')
  const [totalBudget, setTotalBudget] = useState('1000000')
  const [perSymbolBudget, setPerSymbolBudget] = useState('80000')
  const [maxStockPrice, setMaxStockPrice] = useState('')
  const [runs, setRuns] = useState<Run[]>([])
  const [activity, setActivity] = useState<Run[]>([])
  const [submittedRun, setSubmittedRun] = useState<Run | null>(null)
  const [cancelling, setCancelling] = useState('')
  const [loading, setLoading] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [settings, setSettings] = useState<ResearchSettings | null>(null)
  const [settingsSaving, setSettingsSaving] = useState(false)
  const [error, setError] = useState('')

  const assetSymbols = useMemo(() => Array.from(new Set([
    ...watchlist,
    ...positions.map((position) => position.symbol),
  ])), [watchlist, positions])
  const parsedCustomSymbols = useMemo(() => parseSymbols(customSymbols), [customSymbols])
  const selectedSymbols = scope === 'WATCHLIST' ? assetSymbols : scope === 'CUSTOM' ? parsedCustomSymbols : []

  useEffect(() => selectedSymbols.length ? subscribe(selectedSymbols) : undefined, [selectedSymbols.join(','), subscribe])
  useEffect(() => { void api.researchSettings().then(setSettings).catch(() => setSettings(null)) }, [])

  const loadRuns = useCallback(async () => {
    setError('')
    try {
      const [recent, all] = await Promise.allSettled([api.researchRuns(5), api.researchRuns(50)])
      if (recent.status === 'fulfilled') setRuns(recent.value)
      if (all.status === 'fulfilled') setActivity(all.value.filter((run) => ACTIVE.has(run.status.toUpperCase())))
      if (recent.status === 'rejected' && all.status === 'rejected') throw recent.reason
    } catch (reason) { setError(reason instanceof Error ? reason.message : '研究记录加载失败') }
    finally { setLoading(false) }
  }, [])

  usePageRefresh(loadRuns)
  useEffect(() => { setLoading(true); void loadRuns() }, [loadRuns])
  useEffect(() => {
    if (!activity.length) return
    const timer = window.setInterval(() => void loadRuns(), 2500)
    return () => window.clearInterval(timer)
  }, [activity, loadRuns])

  async function submit() {
    const total = positiveNumber(totalBudget)
    const perSymbol = positiveNumber(perSymbolBudget)
    const maxPrice = positiveNumber(maxStockPrice)
    if (scope !== 'MARKET' && !selectedSymbols.length) {
      setError(scope === 'WATCHLIST' ? '自选股与持仓为空，请先添加股票' : '请输入至少一只有效 A 股代码')
      return
    }
    if (!total || !perSymbol) { setError('总资金预算和单股最高投入必须大于 0'); return }
    if (perSymbol > total) { setError('单股最高投入不能超过总资金预算'); return }
    if (maxStockPrice.trim() && !maxPrice) { setError('最高可接受股价必须大于 0'); return }
    setSubmitting(true); setError('')
    try {
      const knownRunIds = new Set([...runs, ...activity].map((run) => run.run_id))
      const result = await api.submitResearch({
        trading_date: date,
        scope,
        symbols: scope === 'CUSTOM' ? selectedSymbols : undefined,
        total_budget: total,
        per_symbol_budget: perSymbol,
        max_stock_price: maxPrice,
      })
      setSubmittedRun({ ...result, reused: result.reused || knownRunIds.has(result.run_id) })
      await loadRuns()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '每日研究提交失败')
    } finally { setSubmitting(false) }
  }

  async function cancel(run: Run) {
    if (!window.confirm(`确认停止研究任务 ${run.run_id}？当前阶段会安全结束后停止。`)) return
    setCancelling(run.run_id); setError('')
    try {
      const updated = await api.cancelResearch(run.run_id)
      setSubmittedRun((current) => current?.run_id === updated.run_id ? updated : current)
      await loadRuns()
    } catch (reason) { setError(reason instanceof Error ? reason.message : '停止任务失败') }
    finally { setCancelling('') }
  }

  async function toggleAutoResearch() {
    if (!settings || settingsSaving) return
    setSettingsSaving(true); setError('')
    try { setSettings(await api.saveResearchSettings(!settings.auto_enabled)) }
    catch (reason) { setError(reason instanceof Error ? reason.message : '自动日研设置保存失败') }
    finally { setSettingsSaving(false) }
  }

  const perSymbol = positiveNumber(perSymbolBudget) || 0
  const maxPrice = positiveNumber(maxStockPrice)
  return <div className="research-layout">
    <Panel title="自动每日研究" eyebrow="PER-USER SCHEDULE">
      <div className="snapshot-callout"><span>{settings?.auto_enabled ? '●' : '○'}</span><div><strong>{settings?.auto_enabled ? '已开启' : '默认关闭'}</strong><p>开启后仅在上海交易日 15:05 起检查收盘数据；未就绪时每 5 分钟重试，最长 2 小时。自动任务固定使用全市场、总资金 100 万元、单股 8 万元。</p></div></div>
      <button className={settings?.auto_enabled ? 'secondary' : 'primary'} disabled={!settings || settingsSaving} onClick={() => void toggleAutoResearch()}>{settingsSaving ? '正在保存…' : settings?.auto_enabled ? '关闭自动日研' : '开启自动日研'}</button>
    </Panel>
    <Panel title="发起每日研究" eyebrow="NEW RESEARCH RUN">
      <div className="run-form research-run-form">
        <label>交易日<input type="date" value={date} max={today()} onChange={(event) => setDate(event.target.value)} /></label>
        <label>研究范围<select value={scope} onChange={(event) => setScope(event.target.value as ResearchScope)}><option value="MARKET">动态市场股票池</option><option value="WATCHLIST">我的自选与持仓</option><option value="CUSTOM">手工指定股票</option></select></label>
        {scope === 'CUSTOM' && <label className="wide">股票代码<textarea rows={3} value={customSymbols} onChange={(event) => setCustomSymbols(event.target.value)} placeholder="600519 或 600519.SH；多个代码用空格、逗号或换行分隔" /><small>已识别 {parsedCustomSymbols.length} 只：{parsedCustomSymbols.join('、') || '—'}</small></label>}
        {scope === 'WATCHLIST' && <div className="snapshot-callout"><span>☆</span><div><strong>将研究 {assetSymbols.length} 只股票</strong><p>{assetSymbols.join('、') || '自选股和我的持仓目前为空'}</p></div></div>}
        <div className="research-budget-grid">
          <label>总资金预算（元）<input type="number" min="1" step="10000" value={totalBudget} onChange={(event) => setTotalBudget(event.target.value)} /></label>
          <label>单股最高投入（元）<input type="number" min="1" step="1000" value={perSymbolBudget} onChange={(event) => setPerSymbolBudget(event.target.value)} /></label>
          <label>最高可接受股价（元）<input type="number" min="0.01" step="0.01" value={maxStockPrice} onChange={(event) => setMaxStockPrice(event.target.value)} placeholder="留空表示不限" /></label>
        </div>
        {scope !== 'MARKET' && selectedSymbols.length > 0 && <div className="research-symbol-preview"><div><strong>预算预览</strong><small>股数按 A 股 100 股一手估算；正式报告使用冻结日线参考价。</small></div><div className="table-wrap"><table><thead><tr><th>证券</th><th>当前价</th><th>单股预算</th><th>估算可买</th><th>价格条件</th></tr></thead><tbody>{selectedSymbols.map((symbol) => {
          const price = quotes[symbol]?.price || 0
          const shares = price > 0 ? Math.floor(perSymbol / price / 100) * 100 : 0
          const allowed = !maxPrice || !price || price <= maxPrice
          return <tr key={symbol}><td><strong>{quotes[symbol]?.name || symbol}</strong><small>{symbol}</small></td><td>{price ? formatNumber(price) : '—'}</td><td>{perSymbol ? formatNumber(perSymbol) : '—'}</td><td>{shares ? `${shares} 股` : '—'}</td><td className={allowed ? 'price-up' : 'price-down'}>{allowed ? '符合' : '超过上限'}</td></tr>
        })}</tbody></table></div></div>}
        <div className="snapshot-callout"><span>▣</span><div><strong>冻结快照模式 · 系统强制开启（只读）</strong><p>不允许关闭。股票范围、预算与数据来源都会写入 Manifest；实时行情只用于表单预览，不进入本次研究。</p></div></div>
        {scope !== 'MARKET' && selectedSymbols.length < 15 && <p className="form-hint">定向标的少于 15 只时只生成逐股评分和研究报告，不生成违反现有 15 股约束的模拟组合。</p>}
        <button className="primary large" onClick={() => void submit()} disabled={submitting}>{submitting ? '正在提交…' : '启动每日研究'}<span>→</span></button>
        {submittedRun && <div className="success-box" role="status"><strong>{submittedRun.reused ? '已复用进行中的研究任务' : '研究任务已提交'}</strong><p>运行 ID：{submittedRun.run_id} · 实际交易日：{submittedRun.trading_date || date} · 状态：{submittedRun.status}</p></div>}
        <ErrorNotice message={error} />
        <p className="form-hint">当日 AKShare 研究默认在 15:05 收盘后开放；完全相同的研究范围与预算会复用已有进行中任务。</p>
      </div>
    </Panel>
    <Panel title="跨日期活动任务" eyebrow="MY ACTIVE RUNS" className="full-span">
      {activity.length ? <div className="research-run-list">{activity.map((run) => {
        const status = run.status.toUpperCase()
        return <article className="research-run-card" key={`active-${run.run_id}`}><div className="research-run-head"><div><strong>{run.trading_date || run.requested_date || '待确定交易日'}</strong><code>{run.run_id}</code></div><StatusPill status={run.status} /></div><div className="run-scope-summary"><span>{run.trigger_source === 'AUTO' ? '自动日研' : '手动研究'}</span><span>{run.phase || '等待流水线更新'}</span><span>进度 {run.progress ?? 0}%</span><span>开始 {formatTime(run.started_at || run.created_at)}</span></div>{status === 'CANCEL_REQUESTED' ? <div className="warning-box"><strong>正在停止</strong><p>当前阶段完成后将安全停止。</p></div> : <button className="secondary" disabled={cancelling === run.run_id} onClick={() => void cancel(run)}>{cancelling === run.run_id ? '正在请求停止…' : '停止任务'}</button>}</article>
      })}</div> : <div className="run-await"><span>✓</span><strong>当前没有活动任务</strong><p>这里会汇总当前用户跨交易日的运行中研究。</p></div>}
    </Panel>
    <Panel title="最近 5 次运行" eyebrow="LIVE PROGRESS" className="full-span recent-run-panel" action={runs[0] && <StatusPill status={runs[0].status} />}>
      {loading && !runs.length ? <Loading /> : runs.length ? <div className="research-run-list recent-run-grid">
        {runs.map((run) => {
          const normalized = run.status.toUpperCase()
          const isActive = ACTIVE.has(normalized)
          const reportLink = `/reports?date=${encodeURIComponent(run.trading_date || date)}&run_id=${encodeURIComponent(run.run_id)}`
          return <article className="research-run-card" key={run.run_id}>
            <div className="research-run-head"><div><strong>{run.trading_date || date}</strong><code>{run.run_id}</code></div><StatusPill status={run.status} /></div>
            <div className="run-scope-summary"><span>{run.trigger_source === 'AUTO' ? '自动日研' : '手动研究'}</span><span>{SCOPE_LABELS[run.research_scope || 'MARKET']}</span>{run.requested_date && run.requested_date !== run.trading_date ? <span>请求 {run.requested_date} → 实际 {run.trading_date}</span> : null}{run.target_symbols?.length ? <span>{run.target_symbols.length} 只指定股票</span> : null}{run.total_budget ? <span>预算 {formatNumber(Number(run.total_budget))} 元</span> : null}</div>
            <div className="progress-track" aria-label={`完成 ${run.progress ?? (isActive ? 5 : 100)}%`}><i className={normalized === 'FAILED' ? 'failed' : isActive ? 'animated' : normalized === 'FUSED' ? 'warning' : 'done'} style={{ width: `${run.progress ?? (isActive ? 5 : 100)}%` }} /></div>
            <div className="research-run-meta"><span>{run.phase || (isActive ? '研究流水线运行中' : '已结束')}</span><span>{formatTime(run.started_at || run.created_at)}</span></div>
            {normalized === 'FAILED' && <div className="failure-box"><strong>运行失败</strong><p>{run.error_message || '未返回具体失败原因，请查看审计事件。'}</p></div>}
            {normalized === 'FUSED' && <div className="warning-box"><strong>观察模式 · {run.reason_code || 'UNSPECIFIED'}</strong><p>{run.reason_message || '正式组合条件未满足'}；正式可用 {run.formal_eligible_count ?? 0} 只，排除 {run.excluded_symbol_count || 0} 只。</p></div>}
            {normalized === 'SUCCEEDED' && <div className="success-box"><strong>研究完成</strong><p>{run.portfolio_generated ? '评分、候选池、预算组合和日报已生成。' : '逐股评分、候选结果和日报已生成；本次范围不要求模拟组合。'}</p></div>}
            {run.report_id && ['SUCCEEDED', 'FUSED'].includes(normalized) && <Link className="report-link" to={reportLink}>打开本次报告 →</Link>}
          </article>
        })}
      </div> : <div className="run-await"><span>✦</span><strong>暂无研究任务</strong><p>启动后会在此展示跨交易日最近 5 次记录。</p></div>}
    </Panel>
    <Panel title="处理边界" eyebrow="DATA CONTRACT" className="full-span">
      <div className="contract-flow"><div><span>01</span><strong>available_at</strong><p>拒绝决策时点后的信息</p></div><i>→</i><div><span>02</span><strong>股票范围与预算</strong><p>随 Manifest 冻结并参与哈希</p></div><i>→</i><div><span>03</span><strong>Agent 结构化输出</strong><p>证据和子分严格校验</p></div><i>→</i><div><span>04</span><strong>确定性评分</strong><p>版本公式生成最终结果</p></div></div>
    </Panel>
  </div>
}
