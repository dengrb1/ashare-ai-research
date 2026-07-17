import { useEffect, useState, type FormEvent } from 'react'
import { api } from '../api'
import { Empty, ErrorNotice, formatTime, Panel, StatusPill } from '../components/Ui'
import type { FinancialSearchResult, FinancialSearchStatus } from '../types'

export function FinancialSearchPage() {
  const [query, setQuery] = useState('贵州茅台股价')
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
    <Panel title="自然语言查询" eyebrow="NEODATA FINANCIAL SEARCH" action={status && <StatusPill status={status.available ? 'ONLINE' : 'FAILED'} />}>
      <form className="financial-search-form" onSubmit={submit}>
        <label>查询股票、指数、板块或金融数据
          <div><input value={query} onChange={(event) => setQuery(event.target.value)} maxLength={256} placeholder="例如：贵州茅台股价、沪深300、600519" required /><button className="primary" disabled={loading}>{loading ? '搜索中…' : '搜索'}</button></div>
        </label>
        <div className="search-examples">
          {['贵州茅台股价', '沪深300', '比亚迪', '600519.SH'].map((item) => <button type="button" key={item} onClick={() => setQuery(item)}>{item}</button>)}
        </div>
        <ErrorNotice message={error} />
      </form>
      {status && <div className="search-provider"><span>默认 Provider</span><strong>{status.provider}</strong><small>{status.mode === 'cli' ? 'NeoData CLI' : '内置兼容模式'} · {status.message}</small></div>}
    </Panel>

    <Panel title="搜索结果" eyebrow="LIVE SEARCH RESULT">
      {result ? <div className="search-results">
        <div className="search-result-meta"><div><span>Provider</span><strong>{result.provider}</strong></div><div><span>实际数据源</span><strong>{result.upstream}</strong></div><div><span>模式</span><strong>{result.mode}</strong></div><div><span>搜索时间</span><strong>{formatTime(result.searched_at)}</strong></div><div><span>耗时</span><strong>{result.elapsed_ms} ms</strong></div></div>
        {result.entities.length > 0 && <div className="search-entities">{result.entities.map((entity, index) => <span key={`${entity.code}-${index}`}>{entity.name} · {entity.code}</span>)}</div>}
        {result.recalls.map((recall, index) => <article className="search-recall" key={`${recall.type}-${index}`}><header><span>{recall.type}</span><strong>{recall.desc}</strong></header><pre>{recall.content}</pre></article>)}
        <div className="snapshot-isolation">实时搜索结果不会写入冻结研究或回测快照；可复现评分仍只使用带 available_at 的确定性输入。</div>
      </div> : <Empty title="等待搜索" description="输入自然语言或证券代码，通过默认 NeoData Provider 查询金融数据。" />}
    </Panel>
  </div>
}
