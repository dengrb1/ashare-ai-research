import { useEffect, useMemo, useState } from 'react'
import { CandlestickChart } from '../components/CandlestickChart'
import { ErrorNotice, formatAmount, formatNumber, Loading, Panel } from '../components/Ui'
import { useMarket, useQuoteSubscription } from '../context/MarketContext'
import type { KlineBar, MarketDataStatus } from '../types'

const PERIODS = ['1m', '5m', '15m', '30m', '60m', 'day']

export function MarketPage() {
  const { watchlist, quotes, addWatch, removeWatch, source, delayed, updatedAt, getKline, loadKline, klineVersion } = useMarket()
  const [symbol, setSymbol] = useState(watchlist[0] || '600519.SH')
  const [search, setSearch] = useState('')
  const [period, setPeriod] = useState('day')
  const [bars, setBars] = useState<KlineBar[]>([])
  const [klineStatus, setKlineStatus] = useState<MarketDataStatus | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  useQuoteSubscription([symbol])
  const quote = quotes[symbol]
  const isWatched = watchlist.includes(symbol)

  useEffect(() => {
    let active = true
    const cached = getKline(symbol, period)
    if (cached) {
      setBars(cached.bars)
      setKlineStatus(cached.status)
      setError(cached.error || '')
      setLoading(false)
    } else {
      setBars([])
      setKlineStatus(null)
      setError('')
      setLoading(true)
    }
    void loadKline(symbol, period).then((entry) => {
      if (!active) return
      setBars(entry.bars)
      setKlineStatus(entry.status)
      setError(entry.error || '')
    }).catch((reason) => {
      if (!active) return
      setKlineStatus(null)
      setError(reason instanceof Error ? reason.message : 'K 线加载失败')
    }).finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [symbol, period, getKline, loadKline, klineVersion])

  const stats = useMemo(() => quote ? [
    ['今开', quote.open], ['最高', quote.high], ['最低', quote.low], ['昨收', quote.prev_close], ['成交量', quote.volume], ['成交额', quote.amount],
  ] : [], [quote])

  function locate() {
    const value = search.trim().toUpperCase()
    if (!value) return
    const normalized = /\.(SH|SZ|BJ)$/.test(value) ? value : `${value}.${value.startsWith('6') ? 'SH' : value.startsWith('8') || value.startsWith('4') ? 'BJ' : 'SZ'}`
    setSymbol(normalized); setSearch('')
  }

  return <div className="market-layout">
    <Panel className="watch-panel" eyebrow="ACTIVE SYMBOLS" title="自选行情" action={<span className="count-badge">{watchlist.length}</span>}>
      <div className="symbol-search"><input value={search} onChange={(event) => setSearch(event.target.value)} onKeyDown={(event) => event.key === 'Enter' && locate()} placeholder="输入代码 600519" /><button onClick={locate}>定位</button></div>
      <div className="watch-list">
        {watchlist.map((item) => { const row = quotes[item]; return <button key={item} className={symbol === item ? 'active' : ''} onClick={() => setSymbol(item)}>
          <div><strong>{row?.name || item}</strong><small>{item}</small></div><div><strong>{formatNumber(row?.price)}</strong><small className={(row?.change_pct || 0) >= 0 ? 'price-up' : 'price-down'}>{row ? `${row.change_pct >= 0 ? '+' : ''}${formatNumber(row.change_pct)}%` : '—'}</small></div>
        </button> })}
      </div>
    </Panel>
    <div className="market-main">
      <Panel className="instrument-panel">
        <div className="instrument-head">
          <div><span className="eyebrow">{symbol}</span><h2>{quote?.name || symbol}</h2></div>
          <div className={`instrument-price ${(quote?.change_pct || 0) >= 0 ? 'price-up' : 'price-down'}`}><strong>{formatNumber(quote?.price)}</strong><span>{quote ? `${quote.change_pct >= 0 ? '+' : ''}${formatNumber(quote.change)}  ${quote.change_pct >= 0 ? '+' : ''}${formatNumber(quote.change_pct)}%` : '等待行情'}</span></div>
          <button className={isWatched ? 'secondary active' : 'secondary'} onClick={() => isWatched ? removeWatch(symbol) : addWatch(symbol)}>{isWatched ? '★ 已自选' : '☆ 加自选'}</button>
        </div>
        <div className="quote-stats">{stats.map(([label, value]) => <div key={label as string}><span>{label}</span><strong>{label === '成交量' || label === '成交额' ? formatAmount(value as number) : formatNumber(value as number)}</strong></div>)}</div>
      </Panel>
      <Panel title="价格走势" eyebrow="ADJUSTED KLINE" action={<div className="period-tabs">{PERIODS.map((item) => <button key={item} className={period === item ? 'active' : ''} onClick={() => setPeriod(item)}>{item === 'day' ? '日线' : item}</button>)}</div>}>
        <div className="data-meta"><span><i className={(klineStatus?.delayed ?? delayed) ? 'warn' : ''} />{klineStatus?.source || source}{(klineStatus?.delayed ?? delayed) ? ' · 非实时缓存' : ' · 实时'}</span><span>后复权</span><span>采集 {klineStatus?.collected_at || updatedAt ? new Date(klineStatus?.collected_at || updatedAt!).toLocaleTimeString('zh-CN', { hour12: false }) : '—'}</span></div>
        <ErrorNotice message={error} />
        {loading && !bars.length ? <Loading label="加载后复权序列" /> : <CandlestickChart bars={bars} />}
      </Panel>
    </div>
  </div>
}
