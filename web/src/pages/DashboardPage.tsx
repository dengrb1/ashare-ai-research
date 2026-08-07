import { useEffect, useState } from 'react'
import { CalendarClock, Calculator, Database, Scale } from 'lucide-react'
import { api, unwrapList } from '../api'
import { MarketClosedNotice } from '../components/MarketClosedNotice'
import { Sparkline } from '../components/Sparkline'
import { useMarket } from '../context/MarketContext'
import type { Notification, Run } from '../types'
import { Empty, formatAmount, formatNumber, formatTime, Panel, StatusPill, today } from '../components/Ui'

const RUNNING = ['PENDING', 'QUEUED', 'RUNNING', 'PROCESSING', 'DATA_READINESS_WAITING', 'CANCEL_REQUESTED']
const COMPLETE = ['SUCCEEDED', 'FUSED', 'COMPLETED', 'SUCCESS']
const FAILED = ['FAILED', 'CANCELLED', 'ERROR']

function researchState(latest: Run | undefined): { label: string; pulse: boolean } {
  if (!latest) return { label: '今日尚未发起研究', pulse: false }
  const status = latest.status.toUpperCase()
  if (COMPLETE.includes(status)) return { label: '今日研究已完成', pulse: false }
  if (RUNNING.includes(status)) {
    return { label: status === 'DATA_READINESS_WAITING' ? '等待基准数据同步' : '研究执行中', pulse: true }
  }
  if (FAILED.includes(status)) return { label: '研究未完成', pulse: false }
  return { label: '等待研究队列', pulse: false }
}

const PRINCIPLES = [
  { icon: CalendarClock, title: '时点正确', text: 'symbol + trading_date + available_at 全程可追溯，决策不晚于数据可用时点' },
  { icon: Calculator, title: '评分确定', text: '最终评分只来自版本化的确定性公式，同一输入产出同一结果' },
  { icon: Scale, title: '规则有效', text: '涨跌停、T+1、费用按生效日期匹配，规则缺失时拒绝交易' },
  { icon: Database, title: '行情隔离', text: '实时数据不进入冻结研究快照，回测与组合只读已提交 Manifest' },
]

export function DashboardPage() {
  const { quotes, watchlist, positions, totalAssets, assetsLoading, quotesLoading, subscribe, getKline, loadKline, klineVersion } = useMarket()
  const [runs, setRuns] = useState<Run[]>([])
  const [notifications, setNotifications] = useState<Notification[]>([])
  useEffect(() => {
    api.runs().then((data) => setRuns(unwrapList(data))).catch(() => setRuns([]))
    api.notificationSummary().then((data) => setNotifications(data.latest || [])).catch(() => setNotifications([]))
  }, [])
  useEffect(() => subscribe(positions.map((position) => position.symbol)), [positions.map((position) => position.symbol).sort().join(','), subscribe])
  const latest = runs[0]
  const quoteSymbols = Array.from(new Set([...watchlist, ...positions.map((position) => position.symbol)])).slice(0, 4)
  useEffect(() => {
    if (assetsLoading || !quoteSymbols.length) return
    const missing = quoteSymbols.filter((symbol) => !(getKline(symbol, 'day', 160)?.bars.length))
    void Promise.all(missing.map(async (symbol) => {
      try { await loadKline(symbol, 'day', 160) } catch { /* quote cards keep their empty state */ }
    }))
  }, [assetsLoading, getKline, klineVersion, loadKline, quoteSymbols.join(',')])
  const completed = runs.filter((run) => COMPLETE.includes(run.status.toUpperCase())).length
  const running = runs.filter((run) => RUNNING.includes(run.status.toUpperCase())).length
  const state = researchState(latest)
  const positionRows = positions.map((position) => {
    const livePrice = quotes[position.symbol]?.price
    const estimated = !(livePrice && livePrice > 0)
    const referencePrice = estimated ? position.cost : livePrice
    const costValue = position.cost * position.quantity
    const marketValue = referencePrice * position.quantity
    return { ...position, referencePrice, costValue, marketValue, pnl: marketValue - costValue, estimated }
  })
  const totalCost = positionRows.reduce((sum, position) => sum + position.costValue, 0)
  const totalMarketValue = positionRows.reduce((sum, position) => sum + position.marketValue, 0)
  const totalPnl = totalMarketValue - totalCost
  const totalReturn = totalCost > 0 ? totalPnl / totalCost * 100 : 0
  const feedItems = notifications.slice(0, 5)

  return <div className="page-stack">
    <section className="research-hero" aria-label="研究时点">
      <div className="research-hero-date">
        <span>研究时点 · POINT IN TIME</span>
        <strong>{latest?.trading_date || today()}</strong>
        <div><i className={state.pulse ? '' : 'idle'} />{state.label}{latest?.decision_at ? ` · 冻结 ${formatTime(latest.decision_at)}` : ''}</div>
      </div>
      <div className="research-hero-stats">
        <div><span>近期完成</span><strong>{completed}</strong><small>次研究 / 回测</small></div>
        <div><span>执行中</span><strong>{running}</strong><small>异步任务</small></div>
        <div><span>自选覆盖</span><strong>{watchlist.length}</strong><small>只活跃刷新</small></div>
      </div>
    </section>
    <MarketClosedNotice />
    <div className="dashboard-overview-grid">
      <div className="metric-grid">
        {quoteSymbols.length ? quoteSymbols.map((symbol) => {
          const quote = quotes[symbol]
          if (quote) {
            const entry = getKline(quote.symbol, 'day', 160)
            const closes = (entry?.bars || []).map((bar) => bar.close).filter((value) => Number.isFinite(value))
            const trendPositive = closes.length > 1 ? closes[closes.length - 1] >= closes[0] : quote.change_pct >= 0
            return <div className="quote-card" key={quote.symbol}>
              <div><div><strong>{quote.name || quote.symbol}</strong><small>{quote.symbol}</small></div><span className={quote.change_pct >= 0 ? 'price-up' : 'price-down'}>{quote.change_pct >= 0 ? '+' : ''}{formatNumber(quote.change_pct)}%</span></div>
              <div className="quote-main"><strong>{formatNumber(quote.price)}</strong></div>
              <div className="quote-card-trend" aria-label={`${quote.name || quote.symbol} 日线走势`}>
                {closes.length > 1 ? <Sparkline values={closes} positive={trendPositive} height={42} /> : <div className="sparkline-placeholder" aria-hidden="true" />}
                <small>{entry?.stale ? '日线稍旧' : '日线走势'}</small>
              </div>
              <small>成交额 {formatAmount(quote.amount)}</small>
            </div>
          }
          return quotesLoading || assetsLoading
            ? <div className="skeleton quote-card" key={symbol} />
            : <div className="quote-card quote-card-empty" key={symbol}><strong>{symbol}</strong><small>行情暂不可用</small></div>
        }) : assetsLoading || quotesLoading ? <><div className="skeleton quote-card" /><div className="skeleton quote-card" /><div className="skeleton quote-card" /><div className="skeleton quote-card" /></> : <div className="quote-card quote-card-empty quote-card-empty-full"><strong>暂无自选行情</strong><small>添加自选股或模拟持仓后显示</small></div>}
      </div>
      <Panel title="关注动态" eyebrow="WATCHLIST FEED" className="dashboard-feed-panel">
        {feedItems.length ? <div className="dashboard-feed-list">
          {feedItems.map((item) => <article className={`dashboard-feed-item severity-${item.severity.toLowerCase()}`} key={item.notification_id}>
            <div><span>{item.notification_type || '研究提醒'}</span><time>{formatTime(item.created_at)}</time></div>
            <strong>{item.title}</strong>
            <p>{item.body}</p>
          </article>)}
        </div> : <div className="dashboard-feed-empty"><strong>暂无新的关注动态</strong><p>自选股提醒、研究结果和系统通知会显示在这里。</p></div>}
      </Panel>
    </div>
    <div className="dashboard-grid">
      <Panel title="模拟持仓盈亏" eyebrow="PAPER HOLDINGS" className="full-span">
        <div className="hero-strip portfolio-summary-strip"><div className="hero-stat"><span>总成本</span><strong>{formatAmount(totalCost)}</strong></div><div className="hero-stat"><span>持仓总市值</span><strong>{formatAmount(totalMarketValue)}</strong><small>{totalAssets ? `账户总资金 ${formatAmount(totalAssets)}` : '未设置账户总资金'}</small></div><div className="hero-stat"><span>总浮动盈亏</span><strong className={totalPnl >= 0 ? 'price-up' : 'price-down'}>{totalPnl >= 0 ? '+' : ''}{formatAmount(totalPnl)}</strong></div><div className="hero-stat"><span>持仓收益率</span><strong className={totalReturn >= 0 ? 'price-up' : 'price-down'}>{totalReturn >= 0 ? '+' : ''}{formatNumber(totalReturn)}%</strong></div></div>
        {assetsLoading ? <div className="holdings-skeleton" role="status" aria-label="持仓数据加载中"><div className="skeleton" /><div className="skeleton" /><div className="skeleton" /><div className="skeleton" /></div> : positionRows.length ? <div className="table-wrap"><table><thead><tr><th>证券</th><th>数量</th><th>参考价</th><th>市值</th><th>浮动盈亏</th><th>收益率</th></tr></thead><tbody>{positionRows.map((position) => <tr key={position.symbol}><td><strong>{quotes[position.symbol]?.name || position.name || position.symbol}</strong><small>{position.symbol}</small></td><td>{position.quantity} 股</td><td>{formatNumber(position.referencePrice)}{position.estimated ? <small>成本价估算</small> : null}</td><td>{formatAmount(position.marketValue)}</td><td className={position.pnl >= 0 ? 'price-up' : 'price-down'}>{position.pnl >= 0 ? '+' : ''}{formatAmount(position.pnl)}</td><td className={position.pnl >= 0 ? 'price-up' : 'price-down'}>{position.costValue > 0 ? `${position.pnl >= 0 ? '+' : ''}${formatNumber(position.pnl / position.costValue * 100)}%` : '—'}</td></tr>)}</tbody></table></div> : <Empty title="暂无模拟持仓" description="在“我的资产”中录入持仓后，这里会每 5 秒刷新参考市值与盈亏" />}
        <p className="form-hint">行情缺失时按持仓成本价估算并明确标注；实时行情不会写入研究快照。</p>
      </Panel>
      <Panel title="研究流水线" eyebrow="DAILY PIPELINE" action={latest && <StatusPill status={latest.status} />}>
        {latest ? <div className="pipeline">
          {['冻结数据快照', '特征与 Agent 分析', '确定性综合评分', '候选过滤', '模拟组合与报告'].map((step, index) => <div className="pipeline-step" key={step}><span>{index + 1}</span><div><strong>{step}</strong><small>{index < 4 ? '依赖前序产物与哈希验证' : '发布可追溯结果'}</small></div></div>)}
          <div className="run-summary"><span>最近运行</span><strong>{latest.run_id}</strong><small>{formatTime(latest.started_at || latest.created_at)}</small></div>
        </div> : <Empty title="尚无研究运行" description="前往每日研究发起首个任务" />}
      </Panel>
      <Panel title="系统原则" eyebrow="GUARDRAILS">
        <div className="guardrail-list">
          {PRINCIPLES.map(({ icon: Icon, title, text }) => <div key={title}><span><Icon size={18} strokeWidth={1.7} /></span><div><strong>{title}</strong><p>{text}</p></div></div>)}
        </div>
      </Panel>
    </div>
  </div>
}
