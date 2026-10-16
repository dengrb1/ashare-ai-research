import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import { Activity, ArrowDown, ArrowUp, BellRing, CandlestickChart, ChevronRight, Pencil, Plus, RefreshCw, SlidersHorizontal, Trash2, X, Sparkles, ListPlus } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { api, openLiveMonitorStream } from '../api'
import { useMarket } from '../context/MarketContext'
import type { LiveMonitorSnapshot, MonitorSignal, PaperPosition, WatchlistGroup } from '../types'
import { CandlestickChart as PriceChart } from '../components/CandlestickChart'
import { Empty, Loading, Panel, StatusPill, formatAmount, formatNumber, formatTime } from '../components/Ui'

const periods = [{ value: '5m', label: '5 分钟' }, { value: '15m', label: '15 分钟' }, { value: 'day', label: '日线' }]

function signalLabel(signal: MonitorSignal) {
  return ({ INTRADAY_DROP: '分时急跌', VOLUME_BREAKOUT: '放量突破', VOLUME_PRICE_DIVERGENCE: '量价背离', MA_DEATH_CROSS: '均线死叉' } as Record<string, string>)[signal.signal_type] || signal.signal_type
}

export function MonitorPage() {
  const reduceMotion = useReducedMotion()
  const { watchlist, positions, totalAssets, quotes, addWatch, removeWatch, reorderWatchlist, upsertPosition, removePosition, saveTotalAssets, refreshSymbol, getKline, loadKline, delayed, assetsSaving, monitoringPolicy } = useMarket()
  const [selected, setSelected] = useState('')
  const [period, setPeriod] = useState('5m')
  const [bars, setBars] = useState(getKline(selected, period, 160)?.bars || [])
  const [signals, setSignals] = useState<MonitorSignal[]>([])
  const [liveMonitor, setLiveMonitor] = useState<LiveMonitorSnapshot | null>(null)
  const [streamConnected, setStreamConnected] = useState(false)
  const [query, setQuery] = useState('')
  const [drawer, setDrawer] = useState(false)
  const [loadingBars, setLoadingBars] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [positionDraft, setPositionDraft] = useState<PaperPosition | null>(null)
  const [previousPositionSymbol, setPreviousPositionSymbol] = useState<string | undefined>()
  const [assetsDraft, setAssetsDraft] = useState(totalAssets == null ? '' : String(totalAssets))
  const [bulkText, setBulkText] = useState('')
  const [assistText, setAssistText] = useState('')
  const [assistPreview, setAssistPreview] = useState<Record<string, unknown> | null>(null)
  const [assistWarnings, setAssistWarnings] = useState<string[]>([])
  const [watchAssistText, setWatchAssistText] = useState('')
  const [assetAssistText, setAssetAssistText] = useState('')
  const [policyAssistText, setPolicyAssistText] = useState('')
  const [policyDraft, setPolicyDraft] = useState({ price_change_pct: 2, volume_multiplier: 3, high_severity_price_change_pct: 5 })
  const [groups, setGroups] = useState<WatchlistGroup[]>([])
  const [newGroupName, setNewGroupName] = useState('')
  const symbols = useMemo(() => Array.from(new Set([...watchlist, ...positions.map((item) => item.symbol)])), [positions, watchlist])
  const activeQuote = selected ? quotes[selected] : undefined

  useEffect(() => {
    if (!selected && symbols[0]) setSelected(symbols[0])
    if (selected && !symbols.includes(selected)) setSelected(symbols[0] || '')
  }, [selected, symbols])

  useEffect(() => {
    if (!selected) return
    let live = true
    setLoadingBars(true)
    setError(null)
    void loadKline(selected, period, 160, true).then((entry) => { if (live) setBars(entry.bars) }).catch((reason) => {
      if (live) setError(reason instanceof Error ? reason.message : 'K 线加载失败')
    }).finally(() => { if (live) setLoadingBars(false) })
    return () => { live = false }
  }, [loadKline, period, selected])

  useEffect(() => {
    if (!symbols.length) return
    let live = true
    const load = () => void api.monitorSignals(symbols, { period, limit: 120 }).then((payload) => { if (live) setSignals(payload?.items || []) }).catch(() => undefined)
    load()
    const timer = window.setInterval(load, 30_000)
    return () => { live = false; window.clearInterval(timer) }
  }, [period, symbols])

  useEffect(() => { setAssetsDraft(totalAssets == null ? '' : String(totalAssets)) }, [totalAssets])
  useEffect(() => { if (monitoringPolicy) setPolicyDraft({ price_change_pct: monitoringPolicy.price_change_pct, volume_multiplier: monitoringPolicy.volume_multiplier, high_severity_price_change_pct: monitoringPolicy.high_severity_price_change_pct }) }, [monitoringPolicy])
  useEffect(() => { void api.assets().then((state) => setGroups(state.watchlist_groups || [])).catch(() => undefined) }, [watchlist.length])

  const refreshGroups = async (statePromise: Promise<{ watchlist_groups?: WatchlistGroup[] }>) => {
    const state = await statePromise
    setGroups(state.watchlist_groups || [])
  }

  useEffect(() => {
    if (!positions.length) {
      setLiveMonitor(null)
      setStreamConnected(false)
      return
    }
    let live = true
    let streamReady = false
    const mergePosition = (payload: Record<string, unknown>) => {
      const symbol = String(payload.symbol || '')
      if (!symbol) return
      setLiveMonitor((current) => {
        if (!current) return current
        const next = current.positions.map((item) => item.symbol === symbol ? { ...item, ...payload, quote_status: (payload.quote_status as typeof item.quote_status) || item.quote_status, evidence: (payload.evidence as Record<string, unknown>) || item.evidence } : item)
        return { ...current, positions: next }
      })
    }
    void api.monitorLive({ includeWatchlist: true }).then((snapshot) => { if (live) setLiveMonitor(snapshot) }).catch(() => undefined)
    const close = openLiveMonitorStream({ interval: 60, includeWatchlist: true }, (event) => {
      if (!live) return
      if (event.type === 'STATUS') {
        streamReady = true
        setStreamConnected(true)
        setLiveMonitor((current) => current ? { ...current, generated_at: String(event.payload.generated_at || current.generated_at), decision_at: String(event.payload.decision_at || current.decision_at), stale: Boolean(event.payload.stale), market_error: typeof event.payload.market_error === 'string' ? event.payload.market_error : null } : current)
      } else if (event.type === 'QUOTE' && event.payload.risk_state) {
        mergePosition(event.payload)
      } else if (event.type === 'RISK') {
        mergePosition(event.payload)
      }
    }, () => { if (live) setStreamConnected(false) })
    const fallback = window.setInterval(() => {
      if (!streamReady) void api.monitorLive({ includeWatchlist: true }).then((snapshot) => { if (live) setLiveMonitor(snapshot) }).catch(() => undefined)
    }, 60_000)
    return () => { live = false; close(); window.clearInterval(fallback) }
  }, [positions.length])

  async function addSymbol(event: React.FormEvent) {
    event.preventDefault()
    const value = query.trim().toUpperCase()
    if (!value) return
    try { await addWatch(value); setSelected(value); setQuery('') } catch (reason) { setError(reason instanceof Error ? reason.message : '添加失败') }
  }
  const submitBulkWatchlist = async (event: React.FormEvent) => {
    event.preventDefault()
    if (!bulkText.trim()) return
    try {
      const parsed = await api.parseWatchlist(bulkText)
      await api.bulkWatchlist({ symbols: parsed.symbols, operation: 'ADD' })
      window.location.reload()
    } catch (reason) { setError(reason instanceof Error ? reason.message : '批量自选保存失败') }
  }
  const runPositionAssist = async () => {
    if (!assistText.trim()) return
    try {
      const result = await api.assistForm({ form_type: 'POSITION', text: assistText })
      setAssistPreview(result.draft); setAssistWarnings(result.warnings)
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'AI 填报失败') }
  }
  const runWatchlistAssist = async () => {
    if (!watchAssistText.trim()) return
    try { const result = await api.assistForm({ form_type: 'WATCHLIST', text: watchAssistText }); setBulkText(String((result.draft.symbols as string[] | undefined || []).join('\n'))); setAssistWarnings(result.warnings) } catch (reason) { setError(reason instanceof Error ? reason.message : 'AI 自选解析失败') }
  }
  const runAssetAssist = async () => {
    if (!assetAssistText.trim()) return
    try { const result = await api.assistForm({ form_type: 'TOTAL_ASSETS', text: assetAssistText }); if (typeof result.draft.total_assets === 'number') setAssetsDraft(String(result.draft.total_assets)); setAssistWarnings(result.warnings) } catch (reason) { setError(reason instanceof Error ? reason.message : 'AI 总资产解析失败') }
  }
  const runPolicyAssist = async () => {
    if (!policyAssistText.trim()) return
    try { const result = await api.assistForm({ form_type: 'MONITOR', text: policyAssistText, draft: policyDraft }); const next = result.draft; setPolicyDraft({ price_change_pct: Number(next.price_change_pct ?? policyDraft.price_change_pct), volume_multiplier: Number(next.volume_multiplier ?? policyDraft.volume_multiplier), high_severity_price_change_pct: Number(next.high_severity_price_change_pct ?? policyDraft.high_severity_price_change_pct) }); setAssistWarnings(result.warnings) } catch (reason) { setError(reason instanceof Error ? reason.message : 'AI 监测参数解析失败') }
  }
  const savePolicy = async () => {
    try { await api.saveMonitoringPolicy({ ...(monitoringPolicy || {}), ...policyDraft, interval_seconds: 60 }); setError(null) } catch (reason) { setError(reason instanceof Error ? reason.message : '监测规则保存失败') }
  }

  const selectedSignals = signals.filter((item) => item.symbol === selected)
  const startPosition = (position?: PaperPosition) => {
    setPreviousPositionSymbol(position?.symbol)
    setPositionDraft(position ? { ...position } : { symbol: selected || '', name: '', quantity: 0, cost: 0, acquired_on: new Date().toISOString().slice(0, 10), stop_loss_mode: 'MANUAL', stop_loss_enabled: true, stop_loss_price: null, profit_trigger_amount: null })
  }
  const submitPosition = async (event: React.FormEvent) => {
    event.preventDefault()
    if (!positionDraft?.symbol.trim()) return
    try { await upsertPosition({ ...positionDraft, symbol: positionDraft.symbol.toUpperCase(), name: positionDraft.name || positionDraft.symbol }); setPositionDraft(null); setPreviousPositionSymbol(undefined) } catch (reason) { setError(reason instanceof Error ? reason.message : '持仓保存失败') }
  }
  const submitAssets = async (event: React.FormEvent) => {
    event.preventDefault()
    const value = assetsDraft.trim() === '' ? null : Number(assetsDraft)
    if (value !== null && (!Number.isFinite(value) || value < 0)) return setError('总资产必须是非负数字')
    try { await saveTotalAssets(value); setError(null) } catch (reason) { setError(reason instanceof Error ? reason.message : '总资产保存失败') }
  }
  return <div className="page-stack monitor-page">
    <section className="monitor-commandbar">
      <div><div className="eyebrow">LIVE RESEARCH MONITOR</div><div className="commandbar-title">盘面监控</div><p>{delayed ? '行情使用最近成功缓存' : '实时行情与冻结研究信号保持隔离'}</p></div>
      <div className="symbol-entry"><form onSubmit={addSymbol}><input aria-label="添加自选股" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="输入 600519.SH" /><button className="primary icon-text" aria-label="添加自选股"><Plus size={16} />添加</button></form><form onSubmit={submitBulkWatchlist}><input aria-label="批量添加自选股" value={bulkText} onChange={(event) => setBulkText(event.target.value)} placeholder="粘贴多个代码，用空格/换行分隔" /><button className="secondary icon-text"><ListPlus size={15} />批量加入</button></form><div className="ai-fill-row compact"><input value={watchAssistText} onChange={(event) => setWatchAssistText(event.target.value)} placeholder="也可描述要关注的股票" /><button type="button" className="secondary icon-text" onClick={() => void runWatchlistAssist()}><Sparkles size={15} />AI 解析</button></div></div>
    </section>
    <div className="monitor-grid">
      <Panel title="自选与持仓" eyebrow="WATCHLIST" className="watchlist-panel">
        {symbols.length ? <div className="watchlist">
          <AnimatePresence initial={false}>
            {symbols.map((symbol) => {
              const quote = quotes[symbol]
              const held = positions.some((item) => item.symbol === symbol)
              const watchIndex = watchlist.indexOf(symbol)
              return <motion.div layout={!reduceMotion} initial={reduceMotion ? false : { opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={reduceMotion ? undefined : { opacity: 0, height: 0 }} className={`watch-row ${selected === symbol ? 'selected' : ''}`} key={symbol}>
                <button className="watch-main" onClick={() => setSelected(symbol)}><span className="symbol-dot" /><span className="watch-name"><strong>{symbol}</strong><small>{held ? '持仓' : '自选'}</small></span><span className={`watch-price ${(quote?.change_pct || 0) >= 0 ? 'positive' : 'negative'}`}>{formatNumber(quote?.price)}</span><span className="watch-change">{quote?.change_pct == null ? '—' : `${quote.change_pct >= 0 ? '+' : ''}${formatNumber(quote.change_pct)}%`}</span><ChevronRight size={15} /></button>
                <span className="watch-actions">{watchIndex >= 0 && <><button className="icon-button" title="上移自选" disabled={watchIndex <= 0 || assetsSaving} onClick={() => void reorderWatchlist(watchlist.map((item, itemIndex) => itemIndex === watchIndex - 1 ? symbol : itemIndex === watchIndex ? watchlist[watchIndex - 1] : item))}><ArrowUp size={13} /></button><button className="icon-button" title="下移自选" disabled={watchIndex >= watchlist.length - 1 || assetsSaving} onClick={() => void reorderWatchlist(watchlist.map((item, itemIndex) => itemIndex === watchIndex ? watchlist[watchIndex + 1] : itemIndex === watchIndex + 1 ? symbol : item))}><ArrowDown size={13} /></button><button className="icon-button danger" title="删除自选" disabled={assetsSaving} onClick={() => void removeWatch(symbol)}><Trash2 size={13} /></button></>}</span>
              </motion.div>
            })}
          </AnimatePresence>
        </div> : <Empty title="还没有监控标的" description="从上方添加自选股" />}
      </Panel>
      <Panel title={selected || '选择标的'} eyebrow="MARKET PULSE" action={<div className="panel-controls"><select aria-label="K 线周期" value={period} onChange={(event) => setPeriod(event.target.value)}>{periods.map((item) => <option value={item.value} key={item.value}>{item.label}</option>)}</select><button className="icon-button" title="刷新行情" onClick={() => selected && void refreshSymbol(selected, true)}><RefreshCw size={15} /></button><button className="icon-button" title="打开量化诊断" onClick={() => setDrawer(true)}><SlidersHorizontal size={15} /></button></div>} className="chart-panel">
        {loadingBars ? <Loading label="加载 K 线" /> : <PriceChart bars={bars} period={period} />}
        {error && <div className="inline-error">{error}</div>}
        {activeQuote && <div className="quote-strip"><span><small>最新价</small><strong>{formatNumber(activeQuote.price)}</strong></span><span><small>涨跌幅</small><strong className={(activeQuote.change_pct || 0) >= 0 ? 'positive' : 'negative'}>{formatNumber(activeQuote.change_pct)}%</strong></span><span><small>成交额</small><strong>{formatAmount(activeQuote.amount)}</strong></span><span><small>行情状态</small><StatusPill status={activeQuote.status?.stale ? 'WARNING' : 'ACTIVE'} /></span></div>}
      </Panel>
      <Panel title="告警流" eyebrow="DETERMINISTIC SIGNALS" className="alerts-panel">
        {signals.length ? <div className="alert-stream">{signals.slice(0, 12).map((signal) => <button className="alert-row" key={`${signal.symbol}-${signal.signal_type}-${signal.decision_at}`} onClick={() => { setSelected(signal.symbol); setDrawer(true) }}><span className={`alert-icon ${signal.severity.toLowerCase()}`}><BellRing size={14} /></span><span><strong>{signal.symbol} · {signalLabel(signal)}</strong><small>{formatTime(signal.available_at)} · 置信度 {Math.round(signal.confidence * 100)}%</small></span><span className="alert-severity">{signal.severity}</span></button>)}</div> : <Empty title="暂无触发信号" description="监控服务会按当前周期刷新" />}
      </Panel>
    </div>
    <section className="live-risk-section">
      <Panel title="实时持仓风险" eyebrow="LIVE POSITION RISK" action={<span className={`status-pill ${streamConnected ? 'success' : 'warning'}`}><i />{streamConnected ? 'SSE 已连接' : '轮询模式'}</span>}>
        {liveMonitor?.positions.length ? <div className="live-risk-grid">{liveMonitor.positions.map((item) => <article className={`live-risk-card ${item.risk_level.toLowerCase()}`} key={item.symbol}><header><div><strong>{item.symbol}</strong><small>{item.name}</small></div><span className="status-pill"><i />{item.risk_state}</span></header><div className="live-risk-metrics"><span><small>最新价</small><strong>{formatNumber(item.price ?? undefined)}</strong></span><span><small>浮动盈亏</small><strong className={(item.unrealized_pnl || 0) >= 0 ? 'positive' : 'negative'}>{formatNumber(item.unrealized_pnl ?? undefined)}</strong></span><span><small>止损距离</small><strong>{item.stop_distance_pct == null ? '—' : `${formatNumber(item.stop_distance_pct)}%`}</strong></span></div><small className="live-risk-source">{item.quote_status.stale ? '行情已延迟' : `行情 ${item.quote_status.source}`} · {formatTime(item.available_at)}</small></article>)}</div> : <Empty title="暂无实时持仓数据" description="添加模拟持仓后会显示风险距离和数据新鲜度" />}
      </Panel>
    </section>
    <section className="asset-controls">
      <Panel title="分钟异常规则" eyebrow="1 MINUTE ANOMALY POLICY"><div className="form-grid"><label>涨跌阈值 %<input type="number" min="0.1" step="0.1" value={policyDraft.price_change_pct} onChange={(event) => setPolicyDraft({ ...policyDraft, price_change_pct: Number(event.target.value) })} /></label><label>成交量倍数<input type="number" min="1.1" step="0.1" value={policyDraft.volume_multiplier} onChange={(event) => setPolicyDraft({ ...policyDraft, volume_multiplier: Number(event.target.value) })} /></label><label>高等级涨跌 %<input type="number" min="0.1" step="0.1" value={policyDraft.high_severity_price_change_pct} onChange={(event) => setPolicyDraft({ ...policyDraft, high_severity_price_change_pct: Number(event.target.value) })} /></label></div><div className="ai-fill-row"><input value={policyAssistText} onChange={(event) => setPolicyAssistText(event.target.value)} placeholder="描述你的异常监测偏好" /><button className="secondary icon-text" onClick={() => void runPolicyAssist()}><Sparkles size={15} />AI 生成参数</button></div><button className="secondary" onClick={() => void savePolicy()}>保存规则</button><small className="form-hint">桌面交易时段每分钟评估；手机仅接收高等级推送。</small></Panel>
      <Panel title="自选分组" eyebrow="WATCHLIST GROUPS"><div className="ai-fill-row"><input value={newGroupName} onChange={(event) => setNewGroupName(event.target.value)} placeholder="新分组名称" /><button className="secondary" disabled={!newGroupName.trim()} onClick={() => void refreshGroups(api.createWatchlistGroup({ name: newGroupName.trim() }).then((state) => { setNewGroupName(''); return state }))}>新建分组</button></div>{groups.map((group) => <div className="position-row" key={group.group_id}><div><strong>{group.name}</strong><small>{group.symbols.length} 个标的</small></div><div className="row-actions"><button className="secondary" onClick={() => void refreshGroups(api.updateWatchlistGroup(group.group_id, { name: group.name, symbols: group.symbols, sort_order: group.sort_order, enabled: !group.enabled, monitoring_policy: group.monitoring_policy }))}>{group.enabled ? '停用' : '启用'}</button><button className="icon-button danger" title="删除分组" onClick={() => void refreshGroups(api.deleteWatchlistGroup(group.group_id))}><Trash2 size={14} /></button></div></div>)}</Panel>
      <Panel title="模拟持仓" eyebrow="PAPER PORTFOLIO" action={<div className="row-actions"><button className="secondary icon-text" onClick={() => void runPositionAssist()}><Sparkles size={15} />AI 生成草稿</button><button className="primary icon-text" onClick={() => startPosition()}><Plus size={15} />新增持仓</button></div>}>
        <div className="ai-fill-row"><input value={assistText} onChange={(event) => setAssistText(event.target.value)} placeholder="例如：茅台 100 股，成本 1420；宁德时代 500 股，成本 240" /><small>AI 只生成草稿，确认后再保存</small></div>
        {assistPreview && <div className="ai-preview"><strong>草稿预览</strong><pre>{JSON.stringify(assistPreview, null, 2)}</pre>{assistWarnings.map((warning) => <small key={warning}>{warning}</small>)}<button className="secondary" onClick={() => { const rows = (assistPreview.positions || []) as PaperPosition[]; if (rows.length) { setPositionDraft(rows[0]); setPreviousPositionSymbol(undefined) } }}>打开第一条确认</button></div>}
        <form className="assets-total-form" onSubmit={submitAssets}><label>总资产<input type="number" min="0" step="0.01" value={assetsDraft} onChange={(event) => setAssetsDraft(event.target.value)} placeholder="可选" /></label><button className="secondary" disabled={assetsSaving}>保存总资产</button></form><div className="ai-fill-row compact"><input value={assetAssistText} onChange={(event) => setAssetAssistText(event.target.value)} placeholder="描述总资产金额" /><button className="secondary icon-text" onClick={() => void runAssetAssist()}><Sparkles size={15} />AI 解析</button></div>
        {positions.length ? <div className="position-list">{positions.map((position) => <div className="position-row" key={position.symbol}><div><strong>{position.symbol}</strong><small>{position.name || '未命名'} · {formatNumber(position.quantity, 0)} 股 · 成本 {formatNumber(position.cost)}</small></div><div className="row-actions"><button className="icon-button" title="编辑持仓" onClick={() => startPosition(position)}><Pencil size={14} /></button><button className="icon-button danger" title="删除持仓" onClick={() => void removePosition(position.symbol)}><Trash2 size={14} /></button></div></div>)}</div> : <Empty title="暂无模拟持仓" description="可从这里记录成本、数量和风控参数" />}
      </Panel>
    </section>
    <AnimatePresence>{positionDraft && <motion.div className="drawer-backdrop" initial={reduceMotion ? false : { opacity: 0 }} animate={{ opacity: 1 }} exit={reduceMotion ? undefined : { opacity: 0 }} onMouseDown={(event) => { if (event.target === event.currentTarget) setPositionDraft(null) }}><motion.aside className="diagnostic-drawer position-editor" initial={reduceMotion ? false : { x: 360 }} animate={{ x: 0 }} exit={reduceMotion ? undefined : { x: 360 }}><header><div><div className="eyebrow">PAPER POSITION</div><h2>{previousPositionSymbol ? '编辑持仓' : '新增持仓'}</h2></div><button className="icon-button" title="关闭编辑" onClick={() => setPositionDraft(null)}><X size={18} /></button></header><form className="settings-form" onSubmit={submitPosition}><label>证券代码<input value={positionDraft.symbol} onChange={(event) => setPositionDraft({ ...positionDraft, symbol: event.target.value.toUpperCase() })} placeholder="600519.SH" required /></label><label>名称<input value={positionDraft.name} onChange={(event) => setPositionDraft({ ...positionDraft, name: event.target.value })} /></label><div className="form-grid"><label>数量<input type="number" min="0" step="1" value={positionDraft.quantity} onChange={(event) => setPositionDraft({ ...positionDraft, quantity: Number(event.target.value) })} required /></label><label>成本<input type="number" min="0" step="0.0001" value={positionDraft.cost} onChange={(event) => setPositionDraft({ ...positionDraft, cost: Number(event.target.value) })} required /></label></div><label>买入日<input type="date" value={positionDraft.acquired_on || ''} onChange={(event) => setPositionDraft({ ...positionDraft, acquired_on: event.target.value || null })} /></label><div className="form-grid"><label>止损模式<select value={positionDraft.stop_loss_mode || 'MANUAL'} onChange={(event) => setPositionDraft({ ...positionDraft, stop_loss_mode: event.target.value as PaperPosition['stop_loss_mode'] })}><option value="MANUAL">手动</option><option value="AUTO_ATR20">ATR20</option><option value="FALLBACK_8PCT">8% 回退</option></select></label><label>止损价<input type="number" min="0" step="0.0001" value={positionDraft.stop_loss_price ?? ''} onChange={(event) => setPositionDraft({ ...positionDraft, stop_loss_price: event.target.value === '' ? null : Number(event.target.value) })} /></label></div><label className="checkbox-row"><input type="checkbox" checked={positionDraft.stop_loss_enabled !== false} onChange={(event) => setPositionDraft({ ...positionDraft, stop_loss_enabled: event.target.checked })} />启用止损监控</label><label>止盈触发金额<input type="number" min="0" step="0.01" value={positionDraft.profit_trigger_amount ?? ''} onChange={(event) => setPositionDraft({ ...positionDraft, profit_trigger_amount: event.target.value === '' ? null : Number(event.target.value) })} /></label><button className="primary" disabled={assetsSaving}>保存持仓</button></form></motion.aside></motion.div>}</AnimatePresence>
    <AnimatePresence>{drawer && <motion.div className="drawer-backdrop" initial={reduceMotion ? false : { opacity: 0 }} animate={{ opacity: 1 }} exit={reduceMotion ? undefined : { opacity: 0 }} onMouseDown={(event) => { if (event.target === event.currentTarget) setDrawer(false) }}><motion.aside className="diagnostic-drawer" initial={reduceMotion ? false : { x: 360 }} animate={{ x: 0 }} exit={reduceMotion ? undefined : { x: 360 }}><header><div><div className="eyebrow">QUANT DIAGNOSTICS</div><h2>{selected || '未选择标的'}</h2></div><button className="icon-button" title="关闭诊断" onClick={() => setDrawer(false)}><X size={18} /></button></header>{activeQuote && <div className="diagnostic-metrics"><div><small>价格</small><strong>{formatNumber(activeQuote.price)}</strong></div><div><small>成交量</small><strong>{formatAmount(activeQuote.volume)}</strong></div><div><small>状态</small><strong>{delayed ? '缓存' : '实时'}</strong></div></div>}<h3>当前信号</h3>{selectedSignals.length ? selectedSignals.map((signal) => <article className="diagnostic-signal" key={signal.signal_type}><div><strong>{signalLabel(signal)}</strong><span className={`severity-${signal.severity.toLowerCase()}`}>{signal.severity}</span></div><p>置信度 {Math.round(signal.confidence * 100)}%</p><pre>{JSON.stringify(signal.evidence, null, 2)}</pre></article>) : <Empty title="没有活动信号" />}</motion.aside></motion.div>}</AnimatePresence>
  </div>
}
