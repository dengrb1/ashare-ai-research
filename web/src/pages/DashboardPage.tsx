import { useEffect, useState } from 'react'
import { api, unwrapList } from '../api'
import { useMarket } from '../context/MarketContext'
import type { Run } from '../types'
import { Empty, formatAmount, formatNumber, formatTime, Panel, StatusPill, today } from '../components/Ui'
import { Sparkline } from '../components/Sparkline'

export function DashboardPage() {
  const { quotes, watchlist } = useMarket()
  const [runs, setRuns] = useState<Run[]>([])
  useEffect(() => { api.runs().then((data) => setRuns(unwrapList(data))).catch(() => setRuns([])) }, [])
  const latest = runs[0]
  const quoteRows = watchlist.map((symbol) => quotes[symbol]).filter(Boolean).slice(0, 4)
  const completed = runs.filter((run) => ['COMPLETED', 'SUCCESS', 'SUCCEEDED'].includes(run.status.toUpperCase())).length
  const running = runs.filter((run) => ['PENDING', 'RUNNING', 'PROCESSING', 'QUEUED'].includes(run.status.toUpperCase())).length

  return <div className="page-stack">
    <div className="hero-strip">
      <div><span className="eyebrow">TRADING DATE</span><strong>{today()}</strong><p>数据可用性截止时点与研究决策时点独立记录</p></div>
      <div className="hero-rule" /><div className="hero-stat"><span>近期完成</span><strong>{completed}</strong><small>次研究 / 回测</small></div>
      <div className="hero-stat"><span>执行中</span><strong>{running}</strong><small>异步任务</small></div>
      <div className="hero-stat"><span>自选覆盖</span><strong>{watchlist.length}</strong><small>只活跃刷新</small></div>
    </div>
    <div className="metric-grid">
      {quoteRows.length ? quoteRows.map((quote, index) => <div className="quote-card" key={quote.symbol}>
        <div><div><strong>{quote.name || quote.symbol}</strong><small>{quote.symbol}</small></div><span className={quote.change_pct >= 0 ? 'price-up' : 'price-down'}>{quote.change_pct >= 0 ? '+' : ''}{formatNumber(quote.change_pct)}%</span></div>
        <div className="quote-main"><strong>{formatNumber(quote.price)}</strong><Sparkline positive={quote.change_pct >= 0} values={[quote.prev_close || quote.price * .99, quote.open || quote.price, quote.low || quote.price * .98, quote.high || quote.price * 1.01, quote.price + index * .01]} /></div>
        <small>成交额 {formatAmount(quote.amount)}</small>
      </div>) : <><div className="skeleton quote-card" /><div className="skeleton quote-card" /><div className="skeleton quote-card" /><div className="skeleton quote-card" /></>}
    </div>
    <div className="dashboard-grid">
      <Panel title="研究流水线" eyebrow="DAILY PIPELINE" action={latest && <StatusPill status={latest.status} />}>
        {latest ? <div className="pipeline">
          {['冻结数据快照', '特征与 Agent 分析', '确定性综合评分', '候选过滤', '模拟组合与报告'].map((step, index) => <div className="pipeline-step" key={step}><span>{index + 1}</span><div><strong>{step}</strong><small>{index < 4 ? '依赖前序产物与哈希验证' : '发布可追溯结果'}</small></div><i /></div>)}
          <div className="run-summary"><span>最近运行</span><strong>{latest.run_id}</strong><small>{formatTime(latest.started_at || latest.created_at)}</small></div>
        </div> : <Empty title="尚无研究运行" description="前往每日研究发起首个任务" />}
      </Panel>
      <Panel title="系统原则" eyebrow="GUARDRAILS">
        <div className="guardrail-list">
          <div><span>01</span><div><strong>时点正确</strong><p>symbol + trading_date + available_at</p></div></div>
          <div><span>02</span><div><strong>评分确定</strong><p>仅版本化纯函数生成最终分</p></div></div>
          <div><span>03</span><div><strong>规则有效</strong><p>涨跌停、T+1 与费用按生效日匹配</p></div></div>
          <div><span>04</span><div><strong>行情隔离</strong><p>实时数据不进入冻结研究快照</p></div></div>
        </div>
      </Panel>
    </div>
  </div>
}
