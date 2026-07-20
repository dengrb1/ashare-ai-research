import { useEffect, useMemo, useState } from 'react'
import { Empty, ErrorNotice, formatAmount, formatNumber, Loading, Panel } from '../components/Ui'
import { useMarket, useQuoteSubscription } from '../context/MarketContext'
import type { PaperPosition } from '../types'

type PositionDraft = {
  symbol: string
  name: string
  quantity: string
  cost: string
  acquiredOn: string
  profitTrigger: string
  previousSymbol?: string
}

const EMPTY_POSITION: PositionDraft = { symbol: '', name: '', quantity: '', cost: '', acquiredOn: '', profitTrigger: '' }

export function AssetsPage() {
  const { watchlist, positions, totalAssets, exitMonitorEnabled, defaultProfitTrigger, quotes, assetsLoading, assetsSaving, addWatch, removeWatch, reorderWatchlist, upsertPosition, removePosition, saveTotalAssets, saveExitSettings } = useMarket()
  const [watchSymbol, setWatchSymbol] = useState('')
  const [draft, setDraft] = useState<PositionDraft | null>(null)
  const [totalAssetsDraft, setTotalAssetsDraft] = useState('')
  const [profitTriggerDraft, setProfitTriggerDraft] = useState('')
  const [saving, setSaving] = useState(false)
  const [dragSymbol, setDragSymbol] = useState('')
  const [dragOverSymbol, setDragOverSymbol] = useState('')
  const [formError, setFormError] = useState('')
  useQuoteSubscription(positions.map((row) => row.symbol))

  useEffect(() => {
    setTotalAssetsDraft(totalAssets === null ? '' : String(totalAssets))
  }, [totalAssets])

  useEffect(() => { setProfitTriggerDraft(defaultProfitTrigger === null ? '' : String(defaultProfitTrigger)) }, [defaultProfitTrigger])

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
    setDraft({ symbol: position.symbol, name: position.name, quantity: String(position.quantity), cost: String(position.cost), acquiredOn: position.acquired_on || '', profitTrigger: position.profit_trigger_amount == null ? '' : String(position.profit_trigger_amount), previousSymbol: position.symbol })
  }

  async function savePosition() {
    if (!draft) return
    const position: PaperPosition = { symbol: draft.symbol.trim().toUpperCase(), name: draft.name.trim(), quantity: Number(draft.quantity), cost: Number(draft.cost), acquired_on: draft.acquiredOn || null, profit_trigger_amount: draft.profitTrigger ? Number(draft.profitTrigger) : null }
    if (!/^\d{6}\.(SH|SZ|BJ)$/.test(position.symbol)) { setFormError('持仓证券代码格式不正确'); return }
    if (!Number.isInteger(position.quantity) || position.quantity <= 0) { setFormError('持仓数量必须是大于 0 的整数'); return }
    if (!Number.isFinite(position.cost) || position.cost <= 0) { setFormError('成本价必须大于 0'); return }
    if (position.profit_trigger_amount !== null && (!Number.isFinite(Number(position.profit_trigger_amount)) || Number(position.profit_trigger_amount) <= 0)) { setFormError('个股盈利触发金额必须大于 0'); return }
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

  async function persistExitSettings(enabled = exitMonitorEnabled) {
    const amount = profitTriggerDraft.trim() ? Number(profitTriggerDraft) : null
    if (enabled && (!amount || !Number.isFinite(amount) || amount <= 0)) { setFormError('启用监控前请填写大于 0 的默认盈利触发金额'); return }
    setSaving(true); setFormError('')
    try { await saveExitSettings(enabled, amount) }
    catch (error) { setFormError(error instanceof Error ? error.message : '盈利监控设置保存失败') }
    finally { setSaving(false) }
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
    <div className="summary-grid">
      <div><span>账户总资金</span><strong>{hasAccountTotal ? `¥ ${formatAmount(totalAssets)}` : '—'}</strong><small>{hasAccountTotal ? '含持仓市值与可用现金' : '请先填写账户总资金'}</small></div>
      <div><span>已记录持仓市值</span><strong>¥ {formatAmount(totals.market)}</strong><small>{totals.estimatedCount ? `含 ${totals.estimatedCount} 只成本价估算` : '按最新行情计算'}</small></div>
      <div><span>浮动盈亏</span><strong className={totals.market >= totals.cost ? 'price-up' : 'price-down'}>¥ {formatAmount(totals.market - totals.cost)}</strong><small>{formatNumber(pnlPercent)}%</small></div>
      <div><span>可用现金（估算）</span><strong className={availableCash !== null && availableCash < 0 ? 'price-down' : ''}>{availableCash === null ? '—' : `¥ ${formatAmount(availableCash)}`}</strong><small>{availableCash === null ? '填写账户总资金后自动计算' : '账户总资金减已记录持仓市值'}</small></div>
    </div>
    <ErrorNotice message={formError} />
    <Panel title="AI 盈利退出监控" eyebrow="EXIT MONITOR" className="exit-monitor-panel" action={<span className={`monitor-state ${exitMonitorEnabled ? 'enabled' : ''}`}><i />{exitMonitorEnabled ? '监控中' : '已暂停'}</span>}>
      <div className="exit-monitor-form">
        <label className="exit-trigger-field">默认盈利触发金额（元）<input type="number" min="0.01" step="0.01" value={profitTriggerDraft} disabled={busy} onChange={(event) => setProfitTriggerDraft(event.target.value)} /><small>严格超过该金额后才会触发研究</small></label>
        <label className="exit-monitor-toggle"><input type="checkbox" checked={exitMonitorEnabled} disabled={busy} onChange={(event) => void persistExitSettings(event.target.checked)} /><span className="toggle-control" aria-hidden="true" /><span><strong>盘中每 5 分钟自动检查</strong><small>仅在 A 股交易时段运行</small></span></label>
        <button className="primary" disabled={busy} onClick={() => void persistExitSettings()}>保存监控设置</button>
      </div>
      <p className="exit-monitor-note"><span>i</span>个股设置优先于全局阈值；同日价格变化不足 3% 且研究、持仓未变化时复用结论，不重复付费调用。</p>
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
          <label>个股触发金额（可选）<input type="number" min="0.01" step="0.01" value={draft.profitTrigger} onChange={(event) => setDraft({ ...draft, profitTrigger: event.target.value })} /></label>
          <div className="row-actions"><button className="primary" disabled={busy} onClick={() => void savePosition()}>保存</button><button className="secondary" disabled={busy} onClick={() => setDraft(null)}>取消</button></div>
        </div>}
        {positions.length ? <div className="table-wrap"><table><thead><tr><th>标的</th><th>持仓 / 成本</th><th>参考价</th><th>市值</th><th>盈亏</th><th>当前仓位</th><th>操作</th></tr></thead><tbody>
          {positions.map((row) => { const quote = quotes[row.symbol]; const latestPrice = quote?.price; const hasLatestPrice = typeof latestPrice === 'number' && latestPrice > 0; const price = hasLatestPrice ? latestPrice : row.cost; const pnl = (price / row.cost - 1) * 100; const currentWeight = hasAccountTotal ? price * row.quantity / totalAssets * 100 : null; return <tr key={row.symbol}><td><strong>{row.name || quote?.name || row.symbol}</strong><small>{row.symbol}</small></td><td>{row.quantity}<small>¥ {formatNumber(row.cost)} · {row.acquired_on || '未填买入日'}</small></td><td className={hasLatestPrice ? ((quote?.change_pct || 0) >= 0 ? 'price-up' : 'price-down') : ''}>{formatNumber(price)}<small>{hasLatestPrice ? '最新行情' : '成本价估算'}</small></td><td>¥ {formatAmount(price * row.quantity)}</td><td className={pnl >= 0 ? 'price-up' : 'price-down'}>{pnl >= 0 ? '+' : ''}{formatNumber(pnl)}%<small>触发 ¥ {formatAmount(row.profit_trigger_amount == null ? defaultProfitTrigger || undefined : Number(row.profit_trigger_amount))}</small></td><td>{currentWeight === null ? <><span>—</span><small>请先填写账户总资金</small></> : `${formatNumber(currentWeight)}%`}</td><td><div className="row-actions"><button className="secondary" disabled={busy} onClick={() => editPosition(row)}>编辑</button><button className="danger-button" disabled={busy} onClick={() => void deletePosition(row.symbol)}>删除</button></div></td></tr> })}
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
