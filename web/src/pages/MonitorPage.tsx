import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import { Activity, ArrowDown, ArrowUp, BellRing, CandlestickChart, ChevronRight, Pencil, Plus, RefreshCw, SlidersHorizontal, Trash2, X } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { useMarket } from '../context/MarketContext'
import type { MonitorSignal, PaperPosition } from '../types'
import { CandlestickChart as PriceChart } from '../components/CandlestickChart'
import { Empty, Loading, Panel, StatusPill, formatAmount, formatNumber, formatTime } from '../components/Ui'

const periods = [{ value: '5m', label: '5 分钟' }, { value: '15m', label: '15 分钟' }, { value: 'day', label: '日线' }]

function signalLabel(signal: MonitorSignal) {
  return ({ INTRADAY_DROP: '分时急跌', VOLUME_BREAKOUT: '放量突破', VOLUME_PRICE_DIVERGENCE: '量价背离', MA_DEATH_CROSS: '均线死叉' } as Record<string, string>)[signal.signal_type] || signal.signal_type
}

export function MonitorPage() {
  const reduceMotion = useReducedMotion()
  const { watchlist, positions, totalAssets, quotes, addWatch, removeWatch, reorderWatchlist, upsertPosition, removePosition, saveTotalAssets, refreshSymbol, getKline, loadKline, delayed, assetsSaving } = useMarket()
  const [selected, setSelected] = useState('')
  const [period, setPeriod] = useState('5m')
  const [bars, setBars] = useState(getKline(selected, period, 160)?.bars || [])
  const [signals, setSignals] = useState<MonitorSignal[]>([])
  const [query, setQuery] = useState('')
  const [drawer, setDrawer] = useState(false)
  const [loadingBars, setLoadingBars] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [positionDraft, setPositionDraft] = useState<PaperPosition | null>(null)
  const [previousPositionSymbol, setPreviousPositionSymbol] = useState<string | undefined>()
  const [assetsDraft, setAssetsDraft] = useState(totalAssets == null ? '' : String(totalAssets))
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

  async function addSymbol(event: React.FormEvent) {
    event.preventDefault()
    const value = query.trim().toUpperCase()
    if (!value) return
    try { await addWatch(value); setSelected(value); setQuery('') } catch (reason) { setError(reason instanceof Error ? reason.message : '添加失败') }
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
      <form className="symbol-entry" onSubmit={addSymbol}><input aria-label="添加自选股" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="输入 600519.SH" /><button className="primary icon-text" aria-label="添加自选股"><Plus size={16} />添加</button></form>
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
    <section className="asset-controls">
      <Panel title="模拟持仓" eyebrow="PAPER PORTFOLIO" action={<button className="primary icon-text" onClick={() => startPosition()}><Plus size={15} />新增持仓</button>}>
        <form className="assets-total-form" onSubmit={submitAssets}><label>总资产<input type="number" min="0" step="0.01" value={assetsDraft} onChange={(event) => setAssetsDraft(event.target.value)} placeholder="可选" /></label><button className="secondary" disabled={assetsSaving}>保存总资产</button></form>
        {positions.length ? <div className="position-list">{positions.map((position) => <div className="position-row" key={position.symbol}><div><strong>{position.symbol}</strong><small>{position.name || '未命名'} · {formatNumber(position.quantity, 0)} 股 · 成本 {formatNumber(position.cost)}</small></div><div className="row-actions"><button className="icon-button" title="编辑持仓" onClick={() => startPosition(position)}><Pencil size={14} /></button><button className="icon-button danger" title="删除持仓" onClick={() => void removePosition(position.symbol)}><Trash2 size={14} /></button></div></div>)}</div> : <Empty title="暂无模拟持仓" description="可从这里记录成本、数量和风控参数" />}
      </Panel>
    </section>
    <AnimatePresence>{positionDraft && <motion.div className="drawer-backdrop" initial={reduceMotion ? false : { opacity: 0 }} animate={{ opacity: 1 }} exit={reduceMotion ? undefined : { opacity: 0 }} onMouseDown={(event) => { if (event.target === event.currentTarget) setPositionDraft(null) }}><motion.aside className="diagnostic-drawer position-editor" initial={reduceMotion ? false : { x: 360 }} animate={{ x: 0 }} exit={reduceMotion ? undefined : { x: 360 }}><header><div><div className="eyebrow">PAPER POSITION</div><h2>{previousPositionSymbol ? '编辑持仓' : '新增持仓'}</h2></div><button className="icon-button" title="关闭编辑" onClick={() => setPositionDraft(null)}><X size={18} /></button></header><form className="settings-form" onSubmit={submitPosition}><label>证券代码<input value={positionDraft.symbol} onChange={(event) => setPositionDraft({ ...positionDraft, symbol: event.target.value.toUpperCase() })} placeholder="600519.SH" required /></label><label>名称<input value={positionDraft.name} onChange={(event) => setPositionDraft({ ...positionDraft, name: event.target.value })} /></label><div className="form-grid"><label>数量<input type="number" min="0" step="1" value={positionDraft.quantity} onChange={(event) => setPositionDraft({ ...positionDraft, quantity: Number(event.target.value) })} required /></label><label>成本<input type="number" min="0" step="0.0001" value={positionDraft.cost} onChange={(event) => setPositionDraft({ ...positionDraft, cost: Number(event.target.value) })} required /></label></div><label>买入日<input type="date" value={positionDraft.acquired_on || ''} onChange={(event) => setPositionDraft({ ...positionDraft, acquired_on: event.target.value || null })} /></label><div className="form-grid"><label>止损模式<select value={positionDraft.stop_loss_mode || 'MANUAL'} onChange={(event) => setPositionDraft({ ...positionDraft, stop_loss_mode: event.target.value as PaperPosition['stop_loss_mode'] })}><option value="MANUAL">手动</option><option value="AUTO_ATR20">ATR20</option><option value="FALLBACK_8PCT">8% 回退</option></select></label><label>止损价<input type="number" min="0" step="0.0001" value={positionDraft.stop_loss_price ?? ''} onChange={(event) => setPositionDraft({ ...positionDraft, stop_loss_price: event.target.value === '' ? null : Number(event.target.value) })} /></label></div><label className="checkbox-row"><input type="checkbox" checked={positionDraft.stop_loss_enabled !== false} onChange={(event) => setPositionDraft({ ...positionDraft, stop_loss_enabled: event.target.checked })} />启用止损监控</label><label>止盈触发金额<input type="number" min="0" step="0.01" value={positionDraft.profit_trigger_amount ?? ''} onChange={(event) => setPositionDraft({ ...positionDraft, profit_trigger_amount: event.target.value === '' ? null : Number(event.target.value) })} /></label><button className="primary" disabled={assetsSaving}>保存持仓</button></form></motion.aside></motion.div>}</AnimatePresence>
    <AnimatePresence>{drawer && <motion.div className="drawer-backdrop" initial={reduceMotion ? false : { opacity: 0 }} animate={{ opacity: 1 }} exit={reduceMotion ? undefined : { opacity: 0 }} onMouseDown={(event) => { if (event.target === event.currentTarget) setDrawer(false) }}><motion.aside className="diagnostic-drawer" initial={reduceMotion ? false : { x: 360 }} animate={{ x: 0 }} exit={reduceMotion ? undefined : { x: 360 }}><header><div><div className="eyebrow">QUANT DIAGNOSTICS</div><h2>{selected || '未选择标的'}</h2></div><button className="icon-button" title="关闭诊断" onClick={() => setDrawer(false)}><X size={18} /></button></header>{activeQuote && <div className="diagnostic-metrics"><div><small>价格</small><strong>{formatNumber(activeQuote.price)}</strong></div><div><small>成交量</small><strong>{formatAmount(activeQuote.volume)}</strong></div><div><small>状态</small><strong>{delayed ? '缓存' : '实时'}</strong></div></div>}<h3>当前信号</h3>{selectedSignals.length ? selectedSignals.map((signal) => <article className="diagnostic-signal" key={signal.signal_type}><div><strong>{signalLabel(signal)}</strong><span className={`severity-${signal.severity.toLowerCase()}`}>{signal.severity}</span></div><p>置信度 {Math.round(signal.confidence * 100)}%</p><pre>{JSON.stringify(signal.evidence, null, 2)}</pre></article>) : <Empty title="没有活动信号" />}</motion.aside></motion.div>}</AnimatePresence>
  </div>
}
