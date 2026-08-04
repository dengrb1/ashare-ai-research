import { useEffect, useState } from 'react'
import { api } from '../api'
import { Empty, ErrorNotice, formatNumber, formatTime, Loading, Panel } from '../components/Ui'
import type { AssetState, TradeAdviceMonitor } from '../types'

const numberValue = (value?: number | string | null) => value == null ? '—' : `¥ ${formatNumber(Number(value))}`

function alertLabel(types?: string[]) {
  if (!types?.length) return '监控中'
  const labels: Record<string, string> = {
    BUY_TARGET_HIT: '买入目标命中',
    SELL_TARGET_HIT: '卖出目标命中',
    STOP_LOSS_TRIGGERED: '止损触发',
  }
  return types.map((type) => labels[type] || type).join('、')
}

export function ExitAdvicePage() {
  const [assets, setAssets] = useState<AssetState | null>(null)
  const [rows, setRows] = useState<TradeAdviceMonitor[]>([])
  const [names, setNames] = useState<Map<string, string>>(new Map())
  const [drafts, setDrafts] = useState<Record<string, { buy: string; sell: string }>>({})
  const [selected, setSelected] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    const load = async () => {
      try {
        const nextAssets = await api.assets()
        const symbols = nextAssets?.watchlist || []
        // 名称是尽力而为：行情不可用时退回仅显示代码，不阻塞页面。
        const [monitors, quotes] = await Promise.all([
          api.tradeAdviceMonitors(),
          symbols.length ? api.quotes(symbols).catch(() => []) : Promise.resolve([])
        ])
        if (!active) return
        setAssets(nextAssets); setRows(monitors)
        const nameMap = new Map<string, string>()
        quotes.forEach(q => { if (q.name) nameMap.set(q.symbol, q.name) })
        setNames(nameMap)
        setDrafts(Object.fromEntries(monitors.map(item => [item.symbol, { buy: item.manual_buy_price?.toString() || '', sell: item.manual_sell_price?.toString() || '' }])))
        setSelected(current => monitors.some(item => item.symbol === current) ? current : (monitors[0]?.symbol || nextAssets?.watchlist[0] || ''))
        setError('')
      } catch (reason) { if (active) setError(reason instanceof Error ? reason.message : '交易建议加载失败') }
      finally { if (active) setLoading(false) }
    }
    void load(); const timer = window.setInterval(load, 15_000)
    return () => { active = false; window.clearInterval(timer) }
  }, [])

  const bySymbol = new Map(rows.map(row => [row.symbol, row]))
  const watchlist = assets?.watchlist || []
  const symbol = selected
  const row = bySymbol.get(symbol)
  const enabled = row?.enabled || false
  const draft = drafts[symbol] || { buy: '', sell: '' }
  const nameFor = (code: string) => names.get(code) || code
  const detailTitle = (code: string) => names.get(code) ? `${names.get(code)} ${code}` : code

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
    {loading ? <Loading label="正在读取交易建议" /> : <div className="exit-advice-layout">
      <Panel title="自选股" eyebrow="WATCHLIST">
        <p className="form-hint">仅面向自选股的模拟建议。交易日 09:30 后生成，命中目标时每 5 分钟重复提醒，不会自动交易。</p>
        {!watchlist.length ? <Empty title="暂无自选股" description="请先在行情或资产页面添加自选股。" /> : <div className="advice-list">
          {watchlist.map(item => {
            const itemRow = bySymbol.get(item)
            return <button key={item} className={selected === item ? 'active' : ''} onClick={() => setSelected(item)}>
              <div><strong>{nameFor(item)}</strong><small>{item}{itemRow?.enabled ? ` · AI 买入 ${numberValue(itemRow.ai_buy_price)} · 卖出 ${numberValue(itemRow.ai_sell_price)}` : ' · 未开启自动建议'}</small></div>
              <span className={`monitor-state ${itemRow?.enabled ? 'enabled' : ''}`}><i />{itemRow?.enabled ? '监控中' : '已暂停'}</span>
            </button>
          })}
        </div>}
      </Panel>
      <Panel title="交易建议" eyebrow="BUY · SELL · STOP LOSS">
        {!watchlist.length ? <Empty title="未选择标的" description="添加自选股后即可配置每只标的的买入、卖出与止损提醒。" /> :
          !symbol ? <Empty title="未选择标的" /> :
            <div className="page-stack">
              <div className="split-row">
                <div><strong>{detailTitle(symbol)}</strong><p className="form-hint">{symbol}{row?.generated_at ? ` · 当日建议：${formatTime(row.generated_at)}` : ' · 开启后将在下一个交易日 09:30 后生成建议'}</p></div>
                <label className="automatic-switch"><input type="checkbox" checked={enabled} onChange={event => void save(symbol, event.target.checked)} /><i aria-hidden="true" /><span>开启自动建议</span></label>
              </div>
              {enabled && <div className="summary-grid compact">
                <div><span>AI 买入目标</span><strong>{numberValue(row?.ai_buy_price)}</strong></div>
                <div><span>AI 卖出目标</span><strong>{numberValue(row?.ai_sell_price)}</strong></div>
                <div><span>止损价</span><strong>{numberValue(row?.stop_loss_price)}</strong></div>
                <div><span>提醒状态</span><strong>{alertLabel(row?.last_alert_types)}</strong></div>
              </div>}
              {Boolean(row?.rationale?.summary) && <div className="advice-summary"><strong>AI 说明</strong><p>{String(row?.rationale?.summary)}</p></div>}
              <div className="form-grid">
                <label>自定义买入价<input inputMode="decimal" value={draft.buy} onChange={event => setDrafts(value => ({ ...value, [symbol]: { ...draft, buy: event.target.value } }))} /></label>
                <label>自定义卖出价<input inputMode="decimal" value={draft.sell} onChange={event => setDrafts(value => ({ ...value, [symbol]: { ...draft, sell: event.target.value } }))} /></label>
              </div>
              <div className="row-actions">
                <button className="primary" onClick={() => void save(symbol, enabled)}>保存自定义价格</button>
                <button className="secondary" disabled={!row?.ai_buy_price || !row?.ai_sell_price} onClick={() => void save(symbol, enabled, true)}>采用 AI 价格</button>
              </div>
              <p className="form-hint">模型：{row?.model_name || '—'} · 仅模拟提醒，不自动交易、不构成投资建议。</p>
            </div>}
      </Panel>
    </div>}
  </div>
}
