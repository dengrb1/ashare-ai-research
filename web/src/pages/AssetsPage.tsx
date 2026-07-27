import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { MarketClosedNotice } from '../components/MarketClosedNotice'
import { Empty, ErrorNotice, formatAmount, formatNumber, Loading, Panel } from '../components/Ui'
import { useMarket, useQuoteSubscription } from '../context/MarketContext'
import type { BuyEntryMonitor, PaperPosition } from '../types'

type PositionDraft = {
  symbol: string
  name: string
  quantity: string
  cost: string
  acquiredOn: string
  exitTriggerPrice: string
  stopLossPrice: string
  stopLossEnabled: boolean
  previousSymbol?: string
}
const EMPTY_POSITION: PositionDraft = { symbol: '', name: '', quantity: '', cost: '', acquiredOn: '', exitTriggerPrice: '', stopLossPrice: '', stopLossEnabled: true }

function exitTriggerLabel(position: PaperPosition, defaultProfitTrigger: number | null) {
  if (position.exit_trigger_price != null) return `股价 > ¥ ${formatNumber(Number(position.exit_trigger_price))}`
  const amount = position.profit_trigger_amount == null ? defaultProfitTrigger : Number(position.profit_trigger_amount)
  return amount == null ? '未设置触发线' : `浮盈 > ¥ ${formatAmount(amount)}`
}

function stopLossLabel(position: PaperPosition) {
  if (position.stop_loss_enabled === false) return '止损监控已关闭'
  if (position.stop_loss_price != null) return `止损 ¥ ${formatNumber(Number(position.stop_loss_price))}`
  return position.stop_loss_mode === 'FALLBACK_8PCT' ? '自动止损 · 成本价下 8%' : '自动止损 · ATR20'
}

function buyMonitorStatusLabel(status: string) {
  return ({ ACTIVE: '等待入场区间', TRIGGERED: '已触发提醒', EXPIRED: '已到期', CANCELLED: '已取消', FAILED: '数据异常' } as Record<string, string>)[status] || status
}

export function AssetsPage() {
  const { watchlist, positions, totalAssets, exitMonitorEnabled, defaultProfitTrigger, stopLossMonitorEnabled, buyMonitorEnabled, quotes, assetsLoading, assetsSaving, addWatch, removeWatch, reorderWatchlist, upsertPosition, removePosition, saveTotalAssets, saveExitSettings } = useMarket()
  const [watchSymbol, setWatchSymbol] = useState('')
  const [draft, setDraft] = useState<PositionDraft | null>(null)
  const [totalAssetsDraft, setTotalAssetsDraft] = useState('')
  const [profitTriggerDraft, setProfitTriggerDraft] = useState('')
  const [saving, setSaving] = useState(false)
  const [dragSymbol, setDragSymbol] = useState('')
  const [dragOverSymbol, setDragOverSymbol] = useState('')
  const [formError, setFormError] = useState('')
  const [manualExitSymbol, setManualExitSymbol] = useState('')
  const [buyEntryMonitors, setBuyEntryMonitors] = useState<BuyEntryMonitor[]>([])
  const [buyEntryMonitorError, setBuyEntryMonitorError] = useState('')
  useQuoteSubscription(positions.map((row) => row.symbol))

  useEffect(() => {
    setTotalAssetsDraft(totalAssets === null ? '' : String(totalAssets))
  }, [totalAssets])

  useEffect(() => { setProfitTriggerDraft(defaultProfitTrigger === null ? '' : String(defaultProfitTrigger)) }, [defaultProfitTrigger])

  useEffect(() => {
    let active = true
    void api.buyEntryMonitors().then((rows) => {
      if (active) setBuyEntryMonitors(rows)
    }).catch((error) => {
      if (active) setBuyEntryMonitorError(error instanceof Error ? error.message : '买入区间状态加载失败')
    })
    return () => { active = false }
  }, [])

  const totals = useMemo(() => positions.reduce((acc, row) => {
    const latestPrice = quotes[row.symbol]?.price
    const hasLatestPrice = typeof latestPrice === 'number' && latestPrice > 0
    const price = hasLatestPrice ? latestPrice : row.cost
    acc.market += price * row.quantity
    acc.cost += row.cost * row.quantity
    if (!hasLatestPrice) acc.estimatedCount += 1
    return acc
  }, { market: 0, cost: 0, estimatedCount: 0 }), [positions, quotes])

  async function submitWatch() {
    const symbol = watchSymbol.trim().toUpperCase()
    if (!/^\d{6}\.(SH|SZ|BJ)$/.test(symbol)) {
      setFormError('请输入 600000.SH、000001.SZ 或 430047.BJ 格式的证券代码')
      return
    }
    setSaving(true); setFormError('')
    try { await addWatch(symbol); setWatchSymbol('') }
    catch (error) { setFormError(error instanceof Error ? error.message : '自选保存失败') }
    finally { setSaving(false) }
  }

  function editPosition(position: PaperPosition) {
    setFormError('')
    setDraft({ symbol: position.symbol, name: position.name, quantity: String(position.quantity), cost: String(position.cost), acquiredOn: position.acquired_on || '', exitTriggerPrice: position.exit_trigger_price == null ? '' : String(position.exit_trigger_price), stopLossPrice: position.stop_loss_price == null ? '' : String(position.stop_loss_price), stopLossEnabled: position.stop_loss_enabled !== false, previousSymbol: position.symbol })
  }

  async function savePosition() {
    if (!draft) return
    const position: PaperPosition = { symbol: draft.symbol.trim().toUpperCase(), name: draft.name.trim(), quantity: Number(draft.quantity), cost: Number(draft.cost), acquired_on: draft.acquiredOn || null, exit_trigger_price: draft.exitTriggerPrice ? Number(draft.exitTriggerPrice) : null, stop_loss_price: draft.stopLossPrice ? Number(draft.stopLossPrice) : null, stop_loss_enabled: draft.stopLossEnabled }
    if (!/^\d{6}\.(SH|SZ|BJ)$/.test(position.symbol)) { setFormError('持仓证券代码格式不正确'); return }
    if (!Number.isInteger(position.quantity) || position.quantity <= 0) { setFormError('持仓数量必须是大于 0 的整数'); return }
    if (!Number.isFinite(position.cost) || position.cost <= 0) { setFormError('成本价必须大于 0'); return }
    if (position.exit_trigger_price !== null && (!Number.isFinite(Number(position.exit_trigger_price)) || Number(position.exit_trigger_price) <= 0)) { setFormError('个股股价触发线必须大于 0'); return }
    if (position.stop_loss_price !== null && (!Number.isFinite(Number(position.stop_loss_price)) || Number(position.stop_loss_price) < position.cost * 0.9 || Number(position.stop_loss_price) > position.cost * 0.95)) { setFormError('手动止损价必须在成本价下方 5%–10%'); return }
    setSaving(true); setFormError('')
    try { await upsertPosition(position, draft.previousSymbol); setDraft(null) }
    catch (error) { setFormError(error instanceof Error ? error.message : '持仓保存失败') }
    finally { setSaving(false) }
  }

  async function deletePosition(symbol: string) {
    setSaving(true); setFormError('')
    try { await removePosition(symbol) }
    catch (error) { setFormError(error instanceof Error ? error.message : '持仓删除失败') }
    finally { setSaving(false) }
  }

  async function saveAccountTotal() {
    const value = totalAssetsDraft.trim()
    if (value) {
      const amount = Number(value)
      if (!Number.isFinite(amount) || amount <= 0) { setFormError('账户总资金必须是大于 0 的金额'); return }
      setSaving(true); setFormError('')
      try { await saveTotalAssets(amount) }
      catch (error) { setFormError(error instanceof Error ? error.message : '账户总资金保存失败') }
      finally { setSaving(false) }
      return
    }
    setSaving(true); setFormError('')
    try { await saveTotalAssets(null) }
    catch (error) { setFormError(error instanceof Error ? error.message : '账户总资金保存失败') }
    finally { setSaving(false) }
  }

  async function clearAccountTotal() {
    setSaving(true); setFormError('')
    try { await saveTotalAssets(null) }
    catch (error) { setFormError(error instanceof Error ? error.message : '账户总资金保存失败') }
    finally { setSaving(false) }
  }

  async function persistExitSettings(enabled = exitMonitorEnabled, stopLoss = stopLossMonitorEnabled, buyMonitor = buyMonitorEnabled) {
    const amount = profitTriggerDraft.trim() ? Number(profitTriggerDraft) : null
    if (enabled && (!amount || !Number.isFinite(amount) || amount <= 0)) { setFormError('启用监控前请填写大于 0 的默认浮盈触发金额'); return }
    setSaving(true); setFormError('')
    try { await saveExitSettings(enabled, amount, stopLoss, buyMonitor) }
    catch (error) { setFormError(error instanceof Error ? error.message : '盈利监控设置保存失败') }
    finally { setSaving(false) }
  }

  async function submitManualExit(symbol: string) {
    setManualExitSymbol(symbol); setFormError('')
    try {
      await api.submitManualExitAdvice(symbol)
      setFormError(`${symbol} 的模拟退出研究已提交，可在“卖出建议”查看进度。`)
    } catch (error) { setFormError(error instanceof Error ? error.message : '模拟退出研究提交失败') }
    finally { setManualExitSymbol('') }
  }

  async function deleteWatch(symbol: string) {
    setSaving(true); setFormError('')
    try { await removeWatch(symbol) }
    catch (error) { setFormError(error instanceof Error ? error.message : '自选删除失败') }
    finally { setSaving(false) }
  }

  async function persistWatchOrder(nextOrder: string[]) {
    setSaving(true); setFormError('')
    try { await reorderWatchlist(nextOrder) }
    catch (error) { setFormError(error instanceof Error ? error.message : '自选排序保存失败') }
    finally { setSaving(false); setDragSymbol(''); setDragOverSymbol('') }
  }

  function moveWatch(symbol: string, direction: -1 | 1) {
    const index = watchlist.indexOf(symbol)
    const target = index + direction
    if (index < 0 || target < 0 || target >= watchlist.length) return
    const next = [...watchlist]
    ;[next[index], next[target]] = [next[target], next[index]]
    void persistWatchOrder(next)
  }

  function dropWatch(targetSymbol: string) {
    if (!dragSymbol || dragSymbol === targetSymbol) { setDragSymbol(''); setDragOverSymbol(''); return }
    const next = watchlist.filter((symbol) => symbol !== dragSymbol)
    const targetIndex = next.indexOf(targetSymbol)
    next.splice(targetIndex < 0 ? next.length : targetIndex, 0, dragSymbol)
    void persistWatchOrder(next)
  }

  if (assetsLoading) return <Loading label="正在加载自选与持仓记录" />
  const pnlPercent = totals.cost > 0 ? (totals.market / totals.cost - 1) * 100 : 0
  const hasAccountTotal = totalAssets !== null && totalAssets > 0
  const availableCash = hasAccountTotal ? totalAssets - totals.market : null
  const busy = saving || assetsSaving

  return <div className="page-stack">
    <MarketClosedNotice />
    <div className="summary-grid">
      <div><span>账户总资金</span><strong>{hasAccountTotal ? `¥ ${formatAmount(totalAssets)}` : '—'}</strong><small>{hasAccountTotal ? '含持仓市值与可用现金' : '请先填写账户总资金'}</small></div>
      <div><span>已记录持仓市值</span><strong>¥ {formatAmount(totals.market)}</strong><small>{totals.estimatedCount ? `含 ${totals.estimatedCount} 只成本价估算` : '按最新行情计算'}</small></div>
      <div><span>浮动盈亏</span><strong className={totals.market >= totals.cost ? 'price-up' : 'price-down'}>¥ {formatAmount(totals.market - totals.cost)}</strong><small>{formatNumber(pnlPercent)}%</small></div>
      <div><span>可用现金（估算）</span><strong className={availableCash !== null && availableCash < 0 ? 'price-down' : ''}>{availableCash === null ? '—' : `¥ ${formatAmount(availableCash)}`}</strong><small>{availableCash === null ? '填写账户总资金后自动计算' : '账户总资金减已记录持仓市值'}</small></div>
    </div>
    <ErrorNotice message={formError} />
    <Panel title="AI 退出风险与买入监控" eyebrow="PAPER MONITORING" className="exit-monitor-panel" action={<span className={`monitor-state ${exitMonitorEnabled || stopLossMonitorEnabled || buyMonitorEnabled ? 'enabled' : ''}`}><i />{exitMonitorEnabled || stopLossMonitorEnabled || buyMonitorEnabled ? '监控中' : '已暂停'}</span>}>
      <div className="exit-monitor-form">
        <label className="exit-trigger-field">默认浮盈触发金额（元）<input type="number" min="0.01" step="0.01" value={profitTriggerDraft} disabled={busy} onChange={(event) => setProfitTriggerDraft(event.target.value)} /><small>未设个股股价线时，严格超过该浮盈才触发</small></label>
        <div className="exit-monitor-switches">
          <label className="exit-monitor-toggle"><input type="checkbox" checked={exitMonitorEnabled} disabled={busy} onChange={(event) => void persistExitSettings(event.target.checked)} /><span className="toggle-control" aria-hidden="true" /><span><strong>浮盈退出监控</strong><small>交易日每分钟检查，仅生成模拟退出研究</small></span></label>
          <label className="exit-monitor-toggle"><input type="checkbox" checked={stopLossMonitorEnabled} disabled={busy} onChange={(event) => void persistExitSettings(exitMonitorEnabled, event.target.checked)} /><span className="toggle-control" aria-hidden="true" /><span><strong>止损预警</strong><small>默认开启；以 ATR20 推导止损，绝不自动卖出</small></span></label>
          <label className="exit-monitor-toggle"><input type="checkbox" checked={buyMonitorEnabled} disabled={busy} onChange={(event) => void persistExitSettings(exitMonitorEnabled, stopLossMonitorEnabled, event.target.checked)} /><span className="toggle-control" aria-hidden="true" /><span><strong>自选股买入区间监控</strong><small>仅对正式 BUY Trade Plan 的次交易日入场区间提醒</small></span></label>
        </div>
        <div className="exit-monitor-actions"><button className="primary" disabled={busy} onClick={() => void persistExitSettings()}>保存监控设置</button></div>
      </div>
      <p className="exit-monitor-note"><span>i</span>所有监控都只创建通知和模拟研究；不会修改持仓、不会自动下单。止损价缺失时按后复权 ATR20 推导，并约束在成本价下方 5%–10%。</p>
      <details className="buy-monitor-guide">
        <summary>自选股买入区间监控怎么用？</summary>
        <ol>
          <li>把目标证券加入“我的自选”，并开启上方的“自选股买入区间监控”。</li>
          <li>完成正式研究；只有评分合格且 Trade Plan 明确为 <code>BUY</code> 的标的才会生成监控。</li>
          <li>系统仅在该计划的下一交易日、首次进入建议入场区间时发送通知；它不会自动买入、卖出或改动持仓。</li>
        </ol>
      </details>
      <section className="buy-monitor-status" aria-labelledby="buy-monitor-status-title">
        <div><strong id="buy-monitor-status-title">当前买入区间状态</strong><small>{buyMonitorEnabled ? '只展示由正式 BUY Trade Plan 生成的提醒' : '总开关已关闭，不会生成新的买入区间提醒'}</small></div>
        {buyEntryMonitorError ? <p className="form-hint">{buyEntryMonitorError}</p> : buyEntryMonitors.length ? <div className="buy-monitor-list">
          {buyEntryMonitors.map((item) => <article key={item.monitor_id}>
            <div><strong>{item.symbol}</strong><small>{buyMonitorStatusLabel(item.status)} · 有效日 {item.effective_date}</small></div>
            <div><strong>¥ {formatNumber(Number(item.entry_low))} — ¥ {formatNumber(Number(item.entry_high))}</strong><small>{item.triggered_at ? `已于 ${new Date(item.triggered_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })} 通知` : `到期 ${new Date(item.expires_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })}`}</small></div>
          </article>)}
        </div> : <p className="form-hint">尚无有效入场区间。开启后，等待正式研究完成并生成符合条件的 BUY Trade Plan。</p>}
      </section>
    </Panel>
    <div className="split-grid assets-grid">
      <Panel title="我的持仓" eyebrow="HOLDINGS" action={<button className="secondary" disabled={busy} onClick={() => setDraft({ ...EMPTY_POSITION })}>新增记录</button>}>
        <div className="asset-total-form">
          <label>账户总资金<input type="number" min="0.01" step="0.01" placeholder="例如 100000" value={totalAssetsDraft} disabled={busy} onChange={(event) => setTotalAssetsDraft(event.target.value)} /></label>
          <div className="row-actions"><button className="primary" disabled={busy} onClick={() => void saveAccountTotal()}>保存账户总资金</button>{hasAccountTotal && <button className="secondary" disabled={busy} onClick={() => { setTotalAssetsDraft(''); void clearAccountTotal() }}>清除</button>}</div>
        </div>
        <p className="form-hint">账户总资金包括已持有股票的市值和可用现金；填写后，当前仓位会按参考市值 ÷ 账户总资金自动计算。</p>
        {draft && <div className="asset-editor">
          <label>证券代码<input value={draft.symbol} placeholder="600000.SH" onChange={(event) => setDraft({ ...draft, symbol: event.target.value })} /></label>
          <label>证券名称<input value={draft.name} placeholder="可选" onChange={(event) => setDraft({ ...draft, name: event.target.value })} /></label>
          <label>持仓数量<input type="number" min="1" step="1" value={draft.quantity} onChange={(event) => setDraft({ ...draft, quantity: event.target.value })} /></label>
          <label>成本价<input type="number" min="0.001" step="0.001" value={draft.cost} onChange={(event) => setDraft({ ...draft, cost: event.target.value })} /></label>
          <label>买入日期<input type="date" value={draft.acquiredOn} onChange={(event) => setDraft({ ...draft, acquiredOn: event.target.value })} /></label>
          <label>个股股价触发线（元/股，可选）<input type="number" min="0.001" step="0.001" value={draft.exitTriggerPrice} onChange={(event) => setDraft({ ...draft, exitTriggerPrice: event.target.value })} /></label>
          <label>手动止损价（元/股，可选）<input type="number" min="0.001" step="0.001" value={draft.stopLossPrice} onChange={(event) => setDraft({ ...draft, stopLossPrice: event.target.value })} /></label>
          <label className="toggle-line"><input type="checkbox" checked={draft.stopLossEnabled} onChange={(event) => setDraft({ ...draft, stopLossEnabled: event.target.checked })} />启用该持仓的止损预警</label>
          <div className="row-actions"><button className="primary" disabled={busy} onClick={() => void savePosition()}>保存</button><button className="secondary" disabled={busy} onClick={() => setDraft(null)}>取消</button></div>
        </div>}
        {positions.length ? <div className="table-wrap"><table><thead><tr><th>标的</th><th>持仓 / 成本</th><th>参考价</th><th>市值</th><th>盈亏</th><th>当前仓位</th><th>操作</th></tr></thead><tbody>
          {positions.map((row) => { const quote = quotes[row.symbol]; const latestPrice = quote?.price; const hasLatestPrice = typeof latestPrice === 'number' && latestPrice > 0; const price = hasLatestPrice ? latestPrice : row.cost; const pnl = (price / row.cost - 1) * 100; const currentWeight = hasAccountTotal ? price * row.quantity / totalAssets * 100 : null; return <tr key={row.symbol}><td><strong>{row.name || quote?.name || row.symbol}</strong><small>{row.symbol}</small></td><td>{row.quantity}<small>¥ {formatNumber(row.cost)} · {row.acquired_on || '未填买入日'}</small></td><td className={hasLatestPrice ? ((quote?.change_pct || 0) >= 0 ? 'price-up' : 'price-down') : ''}>{formatNumber(price)}<small>{hasLatestPrice ? '最新行情' : '成本价估算'}</small></td><td>¥ {formatAmount(price * row.quantity)}</td><td className={pnl >= 0 ? 'price-up' : 'price-down'}>{pnl >= 0 ? '+' : ''}{formatNumber(pnl)}%<small>{exitTriggerLabel(row, defaultProfitTrigger)}</small><small>{stopLossLabel(row)}</small></td><td>{currentWeight === null ? <><span>—</span><small>请先填写账户总资金</small></> : `${formatNumber(currentWeight)}%`}</td><td><div className="row-actions"><button className="secondary" disabled={busy} onClick={() => editPosition(row)}>编辑</button><button className="secondary" disabled={busy || manualExitSymbol === row.symbol} onClick={() => void submitManualExit(row.symbol)}>{manualExitSymbol === row.symbol ? '提交中…' : '退出研究'}</button><button className="danger-button" disabled={busy} onClick={() => void deletePosition(row.symbol)}>删除</button></div></td></tr> })}
        </tbody></table></div> : <Empty title="暂无持仓记录" description="点击“新增记录”录入自己的持仓信息" />}
        {availableCash !== null && availableCash < 0 && <div className="warning-box"><strong>已记录持仓市值高于账户总资金</strong><p>这不会阻止保存；请核对账户总资金、持仓数量或参考价格。</p></div>}
      </Panel>
      <Panel title="我的自选" eyebrow="WATCHLIST">
        <div className="asset-watch-form"><input disabled={busy} value={watchSymbol} placeholder="输入证券代码，如 600000.SH" onChange={(event) => setWatchSymbol(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') void submitWatch() }} /><button className="primary" disabled={busy} onClick={() => void submitWatch()}>添加</button></div>
        {watchlist.length ? <div className="compact-list sortable-watchlist">{watchlist.map((symbol, index) => { const quote = quotes[symbol]; return <div key={symbol} className={dragOverSymbol === symbol ? 'drag-over' : ''} onDragOver={(event) => { if (!busy && dragSymbol) { event.preventDefault(); setDragOverSymbol(symbol) } }} onDrop={(event) => { event.preventDefault(); dropWatch(symbol) }}>
          <button className="drag-handle" draggable={!busy} disabled={busy} aria-label={`拖拽排序 ${symbol}`} title="拖拽排序" onDragStart={(event) => { setDragSymbol(symbol); event.dataTransfer.effectAllowed = 'move'; event.dataTransfer.setData('text/plain', symbol) }} onDragEnd={() => { setDragSymbol(''); setDragOverSymbol('') }}>⠿</button>
          <div><strong>{quote?.name || symbol}</strong><small>{symbol}</small></div>
          <div><strong>{formatNumber(quote?.price)}</strong><small className={(quote?.change_pct || 0) >= 0 ? 'price-up' : 'price-down'}>{quote ? `${quote.change_pct >= 0 ? '+' : ''}${formatNumber(quote.change_pct)}%` : '—'}</small></div>
          <div className="watch-order-actions"><button disabled={busy || index === 0} onClick={() => moveWatch(symbol, -1)} aria-label={`上移 ${symbol}`}>↑</button><button disabled={busy || index === watchlist.length - 1} onClick={() => moveWatch(symbol, 1)} aria-label={`下移 ${symbol}`}>↓</button><button disabled={busy} onClick={() => void deleteWatch(symbol)} aria-label={`移除 ${symbol}`}>×</button></div>
        </div>})}</div> : <Empty title="自选列表为空" description="在上方输入证券代码添加" />}
      </Panel>
    </div>
  </div>
}
