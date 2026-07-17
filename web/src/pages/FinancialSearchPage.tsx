import { useEffect, useState, type FormEvent } from 'react'
import { api } from '../api'
import { Empty, ErrorNotice, formatTime, Panel, StatusPill } from '../components/Ui'
import type { FinancialSearchResult, FinancialSearchStatus } from '../types'

export function FinancialSearchPage() {
  const [query, setQuery] = useState('海尔智家最新股价')
  const [status, setStatus] = useState<FinancialSearchStatus | null>(null)
  const [result, setResult] = useState<FinancialSearchResult | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    api.financialSearchStatus().then(setStatus).catch(() => undefined)
  }, [])

  async function submit(event: FormEvent) {
    event.preventDefault()
    setError('')
    setLoading(true)
    try {
      setResult(await api.financialSearch(query))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '金融数据搜索失败')
    } finally {
      setLoading(false)
    }
  }

  return <div className="financial-search-layout">
    <Panel title="自然语言查询" eyebrow="AI INTENT · DETERMINISTIC FACTS" action={status && <StatusPill status={status.reachable ? 'ONLINE' : status.configured ? 'DEGRADED' : 'UNCONFIGURED'} />}>
      <form className="financial-search-form" onSubmit={submit}>
        <label>查询单只股票或指数的行情、估值、K 线和最新财务指标
          <div><input value={query} onChange={(event) => setQuery(event.target.value)} maxLength={256} placeholder="例如：海尔智家最新股价、宁德时代市盈率、贵州茅台最新财报" required /><button className="primary" disabled={loading}>{loading ? '搜索中…' : '搜索'}</button></div>
        </label>
        <div className="search-examples">
          {['海尔智家最新股价', '宁德时代估值', '贵州茅台最新财报', '沪深300月线'].map((item) => <button type="button" key={item} onClick={() => setQuery(item)}>{item}</button>)}
        </div>
        <ErrorNotice message={error} />
      </form>
      {status && <div className="search-provider"><span>查询链路</span><strong>{status.provider}</strong><small>{status.model ? `${status.model} 解析意图` : '直接代码解析'} · {status.message}</small></div>}
    </Panel>

    <Panel title="搜索结果" eyebrow="LIVE SEARCH RESULT">
      {result ? <div className="search-results">
        <div className="search-result-meta"><div><span>Provider</span><strong>{result.provider}</strong></div><div><span>实际数据源</span><strong>{result.upstream}</strong></div><div><span>模式</span><strong>{result.mode}</strong></div><div><span>搜索时间</span><strong>{formatTime(result.searched_at)}</strong></div><div><span>耗时</span><strong>{result.elapsed_ms} ms</strong></div></div>
        {result.entities.length > 0 && <div className="search-entities">{result.entities.map((entity, index) => <span key={`${entity.code}-${index}`}>{entity.name} · {entity.code}</span>)}</div>}
        {result.interpretation && <div className="snapshot-isolation">{result.interpretation}</div>}
        {result.recalls.map((recall, index) => <article className="search-recall" key={`${recall.type}-${index}`}><header><span>{recall.type}</span><strong>{recall.desc}</strong></header><pre>{recall.content}</pre></article>)}
        {!!result.sources?.length && <div className="search-provider"><span>事实来源</span><strong>{result.sources.map((source) => source.source).join('、')}</strong><small>抓取 {formatTime(result.sources[0]?.fetched_at)}{result.sources[0]?.report_date ? ` · 报告期 ${result.sources[0].report_date}` : ''}{result.sources[0]?.notice_date ? ` · 公告日 ${result.sources[0].notice_date}` : ''}</small></div>}
        {!!result.warnings?.length && <ErrorNotice message={result.warnings.join('；')} />}
        <div className="snapshot-isolation">实时搜索结果不会写入冻结研究或回测快照；可复现评分仍只使用带 available_at 的确定性输入。</div>
      </div> : <Empty title="等待搜索" description="AI 只负责把自然语言解析成结构化意图；数值由确定性金融数据源返回。" />}
    </Panel>
  </div>
}
