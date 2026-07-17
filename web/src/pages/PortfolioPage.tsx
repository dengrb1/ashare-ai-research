import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import type { Portfolio } from '../types'
import { Empty, ErrorNotice, formatNumber, Loading, Panel, StatusPill, today } from '../components/Ui'

function positionValue(position: Record<string, unknown>, key: string, fallback = 0) { const value = Number(position[key] ?? fallback); return Number.isFinite(value) ? value : fallback }

export function PortfolioPage() {
  const [date, setDate] = useState(today())
  const [portfolio, setPortfolio] = useState<Portfolio | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  useEffect(() => { setLoading(true); setError(''); api.portfolio(date).then(setPortfolio).catch((reason) => { setPortfolio(null); setError(reason instanceof Error ? reason.message : '组合加载失败') }).finally(() => setLoading(false)) }, [date])
  const positions = portfolio?.positions || []
  const maxWeight = useMemo(() => Math.max(0, ...positions.map((row) => positionValue(row, 'target_weight', positionValue(row, 'weight')))), [positions])
  return <div className="page-stack">
    <div className="summary-grid"><div><span>组合状态</span><strong>{portfolio ? <StatusPill status={portfolio.status} /> : '—'}</strong><small>{portfolio?.effective_trading_date || '等待发布'}</small></div><div><span>持仓数量</span><strong>{positions.length}</strong><small>上限 15 只</small></div><div><span>预计换手</span><strong>{formatNumber((portfolio?.expected_turnover || 0) * 100)}%</strong><small>单日单边上限 20%</small></div><div><span>现金权重</span><strong>{formatNumber((portfolio?.cash_weight || 0) * 100)}%</strong><small>约束后余量</small></div></div>
    <Panel title="目标组合" eyebrow="SIMULATED PORTFOLIO" action={<label className="date-filter">交易日<input type="date" value={date} onChange={(event) => setDate(event.target.value)} /></label>}>
      <ErrorNotice message={error} />
      {loading ? <Loading /> : positions.length ? <div className="portfolio-view"><div className="weight-map">{positions.map((row, index) => { const symbol = String(row.symbol || `证券 ${index + 1}`); const weight = positionValue(row, 'target_weight', positionValue(row, 'weight')); return <div key={symbol} style={{ flex: Math.max(weight, .01), minWidth: 88 }}><span>{symbol}</span><strong>{formatNumber(weight * 100)}%</strong></div> })}<div className="cash-block" style={{ flex: Math.max(portfolio?.cash_weight || 0, .03) }}><span>现金</span><strong>{formatNumber((portfolio?.cash_weight || 0) * 100)}%</strong></div></div><div className="table-wrap"><table><thead><tr><th>证券</th><th>目标权重</th><th>相对最大仓位</th><th>行业</th><th>调仓动作</th></tr></thead><tbody>{positions.map((row, index) => { const weight = positionValue(row, 'target_weight', positionValue(row, 'weight')); return <tr key={String(row.symbol || index)}><td><strong>{String(row.name || row.symbol || '—')}</strong><small>{String(row.symbol || '')}</small></td><td>{formatNumber(weight * 100)}%</td><td><div className="weight-bar"><span style={{ width: `${maxWeight ? weight / maxWeight * 100 : 0}%` }} /></div></td><td>{String(row.industry_code || row.industry || '—')}</td><td>{String(row.action || '持有')}</td></tr> })}</tbody></table></div></div> : <Empty title="该交易日暂无组合" description="完成候选过滤与组合约束后生成" />}
    </Panel>
    {portfolio?.rejection_reasons && portfolio.rejection_reasons.length > 0 && <Panel title="约束与剔除" eyebrow="REJECTIONS"><ul className="reason-list">{portfolio.rejection_reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul></Panel>}
  </div>
}
