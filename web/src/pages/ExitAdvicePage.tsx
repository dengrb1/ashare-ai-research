import { useEffect, useState } from 'react'
import { api } from '../api'
import { Empty, ErrorNotice, formatAmount, formatNumber, formatTime, Loading, Panel, StatusPill } from '../components/Ui'
import type { ExitAdvice } from '../types'

function actionLabel(value?: string | null, status?: string) {
  if (status === 'PENDING' || status === 'QUEUED' || status === 'RUNNING') return '研究中'
  if (status === 'FAILED') return '生成失败'
  if (status === 'UNAVAILABLE') return '模型暂不可用'
  return value === 'SELL' ? '卖出' : value === 'REDUCE' ? '分批减仓' : value === 'HOLD' ? '继续持有' : '等待研究'
}

function failureMessage(row: ExitAdvice) {
  if (row.status === 'UNAVAILABLE' || row.error_message === 'MODEL_UNAVAILABLE') return '模型服务当前不可用，未生成退出研究；持仓没有被修改或自动卖出。'
  return '本次退出研究处理失败，未生成退出建议；持仓没有被修改或自动卖出。'
}

export function ExitAdvicePage() {
  const [rows, setRows] = useState<ExitAdvice[]>([])
  const [selected, setSelected] = useState<ExitAdvice | null>(null)
  const [loading, setLoading] = useState(true)
  const [retrying, setRetrying] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    const load = () => api.exitAdvice().then((items) => {
      if (!active) return
      setRows(items); setSelected((current) => items.find((item) => item.advice_id === current?.advice_id) || items[0] || null); setError('')
    }).catch((reason) => { if (active) setError(reason instanceof Error ? reason.message : '卖出建议加载失败') }).finally(() => { if (active) setLoading(false) })
    void load(); const timer = window.setInterval(load, 15_000)
    return () => { active = false; window.clearInterval(timer) }
  }, [])

  const result = selected?.result || {}
  const ladder = Array.isArray(result.sell_ladder) ? result.sell_ladder as Array<Record<string, unknown>> : []
  const blockers = Array.isArray(result.execution_blockers) ? result.execution_blockers as string[] : []
  const failed = selected?.status === 'FAILED' || selected?.status === 'UNAVAILABLE'

  async function retry() {
    if (!selected || retrying) return
    setRetrying(true); setError('')
    try {
      const created = await api.submitManualExitAdvice(selected.symbol)
      setRows((current) => [created, ...current.filter((item) => item.advice_id !== created.advice_id)])
      setSelected(created)
    } catch (reason) { setError(reason instanceof Error ? reason.message : '重新发起退出研究失败') }
    finally { setRetrying(false) }
  }

  return <div className="page-stack">
    <ErrorNotice message={error} />
    {loading ? <Loading label="正在读取盘中盈利研究" /> : <div className="exit-advice-layout">
      <Panel title="触发记录" eyebrow="PROFIT TRIGGERS">
        {rows.length ? <div className="advice-list">{rows.map((row) => <button key={row.advice_id} className={selected?.advice_id === row.advice_id ? 'active' : ''} onClick={() => setSelected(row)}>
          <div><strong>{String((row.position_snapshot.name as string) || row.symbol)}</strong><small>{row.symbol} · {formatTime(row.decision_at)}</small></div>
          <div><StatusPill status={row.status} /><strong>{actionLabel(row.action, row.status)}</strong><small>浮盈 ¥ {formatAmount(Number(row.unrealized_profit))}</small></div>
        </button>)}</div> : <Empty title="暂无卖出建议" description="在持仓页启用监控并设置盈利触发金额后，交易时段每 5 分钟检查。" />}
      </Panel>
      <Panel title="AI 分档卖出方案" eyebrow="EXIT LADDER">
        {!selected ? <Empty /> : <div className="page-stack">
          <div className="summary-grid compact"><div><span>当前价</span><strong>¥ {formatNumber(Number(selected.current_price))}</strong></div><div><span>触发线</span><strong>¥ {formatAmount(Number(selected.trigger_amount))}</strong></div><div><span>AI 结论</span><strong>{actionLabel(selected.action, selected.status)}</strong></div><div><span>调用</span><strong>{selected.cache_hit ? '缓存命中' : '新生成'}</strong></div></div>
          {typeof result.summary === 'string' && <div className="advice-summary"><strong>研究结论</strong><p>{result.summary}</p></div>}
          {failed ? <div className="failure-box"><strong>{selected.status === 'UNAVAILABLE' ? '模型暂不可用' : '退出研究生成失败'}</strong><p>{failureMessage(selected)}</p><button className="secondary" disabled={retrying} onClick={() => void retry()}>{retrying ? '正在重新发起…' : '重新发起研究'}</button></div> : ladder.length ? <div className="table-wrap"><table><thead><tr><th>目标价</th><th>卖出数量</th><th>预计金额</th><th>状态</th><th>依据</th></tr></thead><tbody>{ladder.map((item, index) => <tr key={index}><td>¥ {item.target_price as string}</td><td>{String(item.quantity)} 股</td><td>¥ {formatAmount(Number(item.estimated_gross_proceeds))}</td><td>{item.status === 'READY_FOR_CONFIRMATION' ? '待用户确认' : '已阻断'}</td><td>{String(item.reason || '')}</td></tr>)}</tbody></table></div> : <Empty title={selected.status === 'PENDING' || selected.status === 'QUEUED' || selected.status === 'RUNNING' ? '研究正在生成' : '暂无可执行分档'} />}
          {!!blockers.length && <div className="warning-box"><strong>模拟卖出门禁未通过</strong><p>{blockers.join('、')}。请补齐买入日期或等待交易规则数据可用；系统不会自动修改持仓。</p></div>}
          <p className="form-hint">模型：{selected.model_name || '—'} · 思考强度：{selected.reasoning_effort || '—'} · 仅模拟方案，不接入实盘。</p>
        </div>}
      </Panel>
    </div>}
  </div>
}
