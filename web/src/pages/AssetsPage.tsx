import { useMemo, useState } from 'react'
import { Empty, ErrorNotice, formatAmount, formatNumber, Loading, Panel } from '../components/Ui'
import { useMarket, useQuoteSubscription } from '../context/MarketContext'
import type { PaperPosition } from '../types'

type PositionDraft = {
  symbol: string
  name: string
  quantity: string
  cost: string
  targetWeight: string
  previousSymbol?: string
}

const EMPTY_POSITION: PositionDraft = { symbol: '', name: '', quantity: '', cost: '', targetWeight: '' }

export function AssetsPage() {
  const { watchlist, positions, quotes, assetsLoading, addWatch, removeWatch, upsertPosition, removePosition } = useMarket()
  const [watchSymbol, setWatchSymbol] = useState('')
  const [draft, setDraft] = useState<PositionDraft | null>(null)
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState('')
  useQuoteSubscription(positions.map((row) => row.symbol))

  const totals = useMemo(() => positions.reduce((acc, row) => {
    const price = quotes[row.symbol]?.price || row.cost
    acc.market += price * row.quantity
    acc.cost += row.cost * row.quantity
    acc.weight += row.target_weight
    return acc
  }, { market: 0, cost: 0, weight: 0 }), [positions, quotes])

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
    setDraft({ symbol: position.symbol, name: position.name, quantity: String(position.quantity), cost: String(position.cost), targetWeight: String(position.target_weight * 100), previousSymbol: position.symbol })
  }

  async function savePosition() {
    if (!draft) return
    const position: PaperPosition = { symbol: draft.symbol.trim().toUpperCase(), name: draft.name.trim(), quantity: Number(draft.quantity), cost: Number(draft.cost), target_weight: Number(draft.targetWeight) / 100 }
    if (!/^\d{6}\.(SH|SZ|BJ)$/.test(position.symbol)) { setFormError('持仓证券代码格式不正确'); return }
    if (!Number.isInteger(position.quantity) || position.quantity <= 0) { setFormError('持仓数量必须是大于 0 的整数'); return }
    if (!Number.isFinite(position.cost) || position.cost <= 0) { setFormError('成本价必须大于 0'); return }
    if (!Number.isFinite(position.target_weight) || position.target_weight < 0 || position.target_weight > 1) { setFormError('目标权重必须在 0% 到 100% 之间'); return }
    const otherWeight = positions.filter((item) => item.symbol !== draft.previousSymbol).reduce((sum, item) => sum + item.target_weight, 0)
    if (otherWeight + position.target_weight > 1.000001) { setFormError('全部持仓的目标权重合计不能超过 100%'); return }
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

  async function deleteWatch(symbol: string) {
    setSaving(true); setFormError('')
    try { await removeWatch(symbol) }
    catch (error) { setFormError(error instanceof Error ? error.message : '自选删除失败') }
    finally { setSaving(false) }
  }

  if (assetsLoading) return <Loading label="正在加载自选与模拟持仓" />
  const pnlPercent = totals.cost > 0 ? (totals.market / totals.cost - 1) * 100 : 0
  const cashWeight = Math.max(0, 1 - totals.weight)

  return <div className="page-stack">
    <div className="summary-grid">
      <div><span>模拟持仓市值</span><strong>¥ {formatAmount(totals.market)}</strong><small>不连接真实交易</small></div>
      <div><span>浮动盈亏</span><strong className={totals.market >= totals.cost ? 'price-up' : 'price-down'}>¥ {formatAmount(totals.market - totals.cost)}</strong><small>{formatNumber(pnlPercent)}%</small></div>
      <div><span>持仓标的</span><strong>{positions.length}</strong><small>最多维护 15 只</small></div>
      <div><span>现金权重</span><strong>{formatNumber(cashWeight * 100)}%</strong><small>按目标权重余量计算</small></div>
    </div>
    <ErrorNotice message={formError} />
    <div className="split-grid assets-grid">
      <Panel title="模拟持仓" eyebrow="POSITIONS" action={<button className="secondary" onClick={() => setDraft({ ...EMPTY_POSITION })}>新增持仓</button>}>
        {draft && <div className="asset-editor">
          <label>证券代码<input value={draft.symbol} placeholder="600000.SH" onChange={(event) => setDraft({ ...draft, symbol: event.target.value })} /></label>
          <label>证券名称<input value={draft.name} placeholder="可选" onChange={(event) => setDraft({ ...draft, name: event.target.value })} /></label>
          <label>持仓数量<input type="number" min="1" step="1" value={draft.quantity} onChange={(event) => setDraft({ ...draft, quantity: event.target.value })} /></label>
          <label>成本价<input type="number" min="0.001" step="0.001" value={draft.cost} onChange={(event) => setDraft({ ...draft, cost: event.target.value })} /></label>
          <label>目标权重（%）<input type="number" min="0" max="100" step="0.01" value={draft.targetWeight} onChange={(event) => setDraft({ ...draft, targetWeight: event.target.value })} /></label>
          <div className="row-actions"><button className="primary" disabled={saving} onClick={() => void savePosition()}>保存</button><button className="secondary" onClick={() => setDraft(null)}>取消</button></div>
        </div>}
        {positions.length ? <div className="table-wrap"><table><thead><tr><th>标的</th><th>持仓 / 成本</th><th>最新价</th><th>市值</th><th>盈亏</th><th>目标权重</th><th>操作</th></tr></thead><tbody>
          {positions.map((row) => { const quote = quotes[row.symbol]; const price = quote?.price || row.cost; const pnl = (price / row.cost - 1) * 100; return <tr key={row.symbol}><td><strong>{row.name || quote?.name || row.symbol}</strong><small>{row.symbol}</small></td><td>{row.quantity}<small>¥ {formatNumber(row.cost)}</small></td><td className={(quote?.change_pct || 0) >= 0 ? 'price-up' : 'price-down'}>{formatNumber(price)}</td><td>¥ {formatAmount(price * row.quantity)}</td><td className={pnl >= 0 ? 'price-up' : 'price-down'}>{pnl >= 0 ? '+' : ''}{formatNumber(pnl)}%</td><td>{formatNumber(row.target_weight * 100)}%</td><td><div className="row-actions"><button className="secondary" onClick={() => editPosition(row)}>编辑</button><button className="danger-button" disabled={saving} onClick={() => void deletePosition(row.symbol)}>删除</button></div></td></tr> })}
        </tbody></table></div> : <Empty title="暂无模拟持仓" description="点击“新增持仓”录入自己的模拟持仓" />}
      </Panel>
      <Panel title="我的自选" eyebrow="WATCHLIST">
        <div className="asset-watch-form"><input value={watchSymbol} placeholder="输入证券代码，如 600000.SH" onChange={(event) => setWatchSymbol(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') void submitWatch() }} /><button className="primary" disabled={saving} onClick={() => void submitWatch()}>添加</button></div>
        {watchlist.length ? <div className="compact-list">{watchlist.map((symbol) => { const quote = quotes[symbol]; return <div key={symbol}><div><strong>{quote?.name || symbol}</strong><small>{symbol}</small></div><div><strong>{formatNumber(quote?.price)}</strong><small className={(quote?.change_pct || 0) >= 0 ? 'price-up' : 'price-down'}>{quote ? `${quote.change_pct >= 0 ? '+' : ''}${formatNumber(quote.change_pct)}%` : '—'}</small></div><button disabled={saving} onClick={() => void deleteWatch(symbol)} aria-label={`移除 ${symbol}`}>×</button></div>})}</div> : <Empty title="自选列表为空" description="在上方输入证券代码添加" />}
      </Panel>
    </div>
  </div>
}
