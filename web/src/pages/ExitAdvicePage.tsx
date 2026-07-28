import { useEffect, useState } from 'react'
import { api } from '../api'
import { Empty, ErrorNotice, formatNumber, formatTime, Loading, Panel } from '../components/Ui'
import type { AssetState, TradeAdviceMonitor } from '../types'

const numberValue = (value?: number | string | null) => value == null ? '—' : `¥ ${formatNumber(Number(value))}`

export function ExitAdvicePage() {
  const [assets, setAssets] = useState<AssetState | null>(null)
  const [rows, setRows] = useState<TradeAdviceMonitor[]>([])
  const [drafts, setDrafts] = useState<Record<string, { buy: string; sell: string }>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    const load = async () => {
      try {
        const [nextAssets, monitors] = await Promise.all([api.assets(), api.tradeAdviceMonitors()])
        if (!active) return
        setAssets(nextAssets); setRows(monitors)
        setDrafts(Object.fromEntries(monitors.map(item => [item.symbol, { buy: item.manual_buy_price?.toString() || '', sell: item.manual_sell_price?.toString() || '' }])))
        setError('')
      } catch (reason) { if (active) setError(reason instanceof Error ? reason.message : '交易建议加载失败') }
      finally { if (active) setLoading(false) }
    }
    void load(); const timer = window.setInterval(load, 15_000)
    return () => { active = false; window.clearInterval(timer) }
  }, [])

  const bySymbol = new Map(rows.map(row => [row.symbol, row]))
  async function save(symbol: string, enabled: boolean, useAi = false) {
    const current = bySymbol.get(symbol)
    const draft = drafts[symbol] || { buy: '', sell: '' }
    const buy = useAi ? Number(current?.ai_buy_price) : (draft.buy ? Number(draft.buy) : null)
    const sell = useAi ? Number(current?.ai_sell_price) : (draft.sell ? Number(draft.sell) : null)
    try {
      const saved = await api.saveTradeAdviceMonitor({ symbol, enabled, manual_buy_price: Number.isFinite(buy) ? buy : null, manual_sell_price: Number.isFinite(sell) ? sell : null })
      setRows(currentRows => [...currentRows.filter(item => item.symbol !== symbol), saved].sort((a, b) => a.symbol.localeCompare(b.symbol)))
      setDrafts(currentDrafts => ({ ...currentDrafts, [symbol]: { buy: saved.manual_buy_price?.toString() || '', sell: saved.manual_sell_price?.toString() || '' } }))
    } catch (reason) { setError(reason instanceof Error ? reason.message : '保存失败') }
  }

  return <div className="page-stack">
    <ErrorNotice message={error} />
    {loading ? <Loading label="正在读取交易建议" /> : <Panel title="交易建议" eyebrow="BUY · SELL · STOP LOSS">
      <p className="form-hint">仅面向自选股的模拟建议。交易日 09:30 后生成，页面实时刷新；命中目标时每 5 分钟持续提醒，不会自动交易。</p>
      {!assets?.watchlist.length ? <Empty title="暂无自选股" description="请先在行情或资产页面添加自选股。" /> : <div className="page-stack">
        {assets.watchlist.map(symbol => {
          const row = bySymbol.get(symbol); const draft = drafts[symbol] || { buy: '', sell: '' }
          return <div className="panel" key={symbol}><div className="split-row"><div><strong>{symbol}</strong><p className="form-hint">{row?.generated_at ? `当日建议：${formatTime(row.generated_at)}` : '开启后将在下一个交易日 09:30 后生成建议'}</p></div><label><input type="checkbox" checked={row?.enabled || false} onChange={event => void save(symbol, event.target.checked)} /> 开启自动建议</label></div>
            {row?.enabled && <div className="summary-grid compact"><div><span>AI 买入目标</span><strong>{numberValue(row.ai_buy_price)}</strong></div><div><span>AI 卖出目标</span><strong>{numberValue(row.ai_sell_price)}</strong></div><div><span>止损价</span><strong>{numberValue(row.stop_loss_price)}</strong></div><div><span>提醒状态</span><strong>{row.last_alert_types.join('、') || '监控中'}</strong></div></div>}
            {Boolean(row?.rationale?.summary) && <p className="form-hint">{String(row?.rationale?.summary)}</p>}
            <div className="form-grid"><label>自定义买入价<input inputMode="decimal" value={draft.buy} onChange={event => setDrafts(value => ({ ...value, [symbol]: { ...draft, buy: event.target.value } }))} /></label><label>自定义卖出价<input inputMode="decimal" value={draft.sell} onChange={event => setDrafts(value => ({ ...value, [symbol]: { ...draft, sell: event.target.value } }))} /></label></div>
            <div className="button-row"><button onClick={() => void save(symbol, row?.enabled || false)}>保存自定义价格</button><button className="secondary" disabled={!row?.ai_buy_price || !row?.ai_sell_price} onClick={() => void save(symbol, row?.enabled || false, true)}>采用 AI 价格</button></div>
          </div>
        })}
      </div>}
    </Panel>}
  </div>
}
