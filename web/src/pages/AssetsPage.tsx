import { useMemo } from 'react'
import { Panel, Empty, formatAmount, formatNumber } from '../components/Ui'
import { useMarket, useQuoteSubscription } from '../context/MarketContext'
import { PAPER_HOLDINGS } from '../marketAssets'

export function AssetsPage() {
  const { watchlist, quotes, removeWatch } = useMarket()
  useQuoteSubscription(PAPER_HOLDINGS.map((row) => row.symbol))
  const totals = useMemo(() => PAPER_HOLDINGS.reduce((acc, row) => {
    const price = quotes[row.symbol]?.price || row.cost
    acc.market += price * row.quantity; acc.cost += row.cost * row.quantity
    return acc
  }, { market: 0, cost: 0 }), [quotes])

  return <div className="page-stack">
    <div className="summary-grid">
      <div><span>模拟持仓市值</span><strong>¥ {formatAmount(totals.market)}</strong><small>不连接真实交易</small></div>
      <div><span>浮动盈亏</span><strong className={totals.market >= totals.cost ? 'price-up' : 'price-down'}>¥ {formatAmount(totals.market - totals.cost)}</strong><small>{formatNumber((totals.market / totals.cost - 1) * 100)}%</small></div>
      <div><span>持仓标的</span><strong>{PAPER_HOLDINGS.length}</strong><small>目标最多 15 只</small></div>
      <div><span>现金权重</span><strong>18.0%</strong><small>模拟组合余量</small></div>
    </div>
    <div className="split-grid">
      <Panel title="模拟持仓" eyebrow="POSITIONS">
        <div className="table-wrap"><table><thead><tr><th>标的</th><th>持仓 / 成本</th><th>最新价</th><th>市值</th><th>盈亏</th><th>目标权重</th></tr></thead><tbody>
          {PAPER_HOLDINGS.map((row) => { const quote = quotes[row.symbol]; const price = quote?.price || row.cost; const pnl = (price / row.cost - 1) * 100; return <tr key={row.symbol}><td><strong>{row.name}</strong><small>{row.symbol}</small></td><td>{row.quantity}<small>¥ {formatNumber(row.cost)}</small></td><td className={(quote?.change_pct || 0) >= 0 ? 'price-up' : 'price-down'}>{formatNumber(price)}</td><td>¥ {formatAmount(price * row.quantity)}</td><td className={pnl >= 0 ? 'price-up' : 'price-down'}>{pnl >= 0 ? '+' : ''}{formatNumber(pnl)}%</td><td>{formatNumber(row.weight * 100)}%</td></tr> })}
        </tbody></table></div>
      </Panel>
      <Panel title="我的自选" eyebrow="WATCHLIST">
        {watchlist.length ? <div className="compact-list">{watchlist.map((symbol) => { const quote = quotes[symbol]; return <div key={symbol}><div><strong>{quote?.name || symbol}</strong><small>{symbol}</small></div><div><strong>{formatNumber(quote?.price)}</strong><small className={(quote?.change_pct || 0) >= 0 ? 'price-up' : 'price-down'}>{quote ? `${quote.change_pct >= 0 ? '+' : ''}${formatNumber(quote.change_pct)}%` : '—'}</small></div><button onClick={() => removeWatch(symbol)} aria-label={`移除 ${symbol}`}>×</button></div>})}</div> : <Empty title="自选列表为空" />}
      </Panel>
    </div>
  </div>
}
