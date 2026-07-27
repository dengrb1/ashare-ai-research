import { useCallback, useEffect, useMemo, useState } from 'react'
import { CandlestickChart } from '../components/CandlestickChart'
import { MarketClosedNotice } from '../components/MarketClosedNotice'
import { ErrorNotice, formatAmount, formatNumber, Loading, Panel } from '../components/Ui'
import { usePageRefresh } from '../context/RefreshContext'
import { useMarket, useQuoteSubscription, type KlineCacheEntry } from '../context/MarketContext'
import { getKlineChunkWindow, getKlineRangePlan, klineCoverage, KLINE_PERIODS, KLINE_RANGES, loadedRangeLabel, trimBarsToRange } from '../marketKlines'
import type { KlineRange } from '../types'

const CHUNK_LIMIT = 1200
const DAILY_REFRESH_MS = 5 * 60 * 1000
const INTRADAY_REFRESH_MS = 30 * 1000

export function MarketPage() {
  const { watchlist, quotes, addWatch, removeWatch, source, delayed, updatedAt, refreshSymbol, refreshRemaining, getKline, loadKline } = useMarket()
  const [symbol, setSymbol] = useState(watchlist[0] || '600519.SH')
  const [search, setSearch] = useState('')
  const [period, setPeriod] = useState('day')
  const [range, setRange] = useState<KlineRange>('1m')
  const [entry, setEntry] = useState<KlineCacheEntry | undefined>()
  const [loading, setLoading] = useState(false)
  const [loadingEarlier, setLoadingEarlier] = useState(false)
  const [noMoreEarlier, setNoMoreEarlier] = useState(false)
  const [coverageMessage, setCoverageMessage] = useState('')
  const [error, setError] = useState('')
  const [rangeClock, setRangeClock] = useState(() => Date.now())
  useQuoteSubscription([symbol])
  const quote = quotes[symbol]
  const isWatched = watchlist.includes(symbol)
  const plan = useMemo(() => getKlineRangePlan(range, new Date(rangeClock)), [range, rangeClock])
  const bars = useMemo(() => trimBarsToRange(entry?.bars || [], plan), [entry?.bars, plan])
  const coverage = useMemo(() => klineCoverage(plan, entry?.requestedStart, bars), [bars, entry?.requestedStart, plan])
  const initialChunk = useMemo(() => getKlineChunkWindow(period, plan), [period, plan])

  const loadWindow = useCallback(async (window: NonNullable<typeof initialChunk>, refreshChunk = false) => loadKline(symbol, period, {
    range,
    start: window.start,
    end: window.end,
    chunk: window.key,
    limit: CHUNK_LIMIT,
    refresh: refreshChunk,
  }), [loadKline, period, range, symbol])

  const forceRefresh = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const klineWork = initialChunk ? loadWindow(initialChunk, true) : Promise.resolve(undefined)
      const [, next] = await Promise.all([refreshSymbol(symbol, true), klineWork])
      if (next) {
        setEntry(next)
        setError(next.error || '')
      }
      // The selected quote and visible K-line are ready before unrelated symbols
      // use the expensive all-market snapshot in the background.
      void refreshRemaining(symbol, true)
    } finally {
      setLoading(false)
    }
  }, [initialChunk, loadWindow, refreshRemaining, refreshSymbol, symbol])
  usePageRefresh(forceRefresh)

  useEffect(() => {
    const refreshClock = () => setRangeClock(Date.now())
    const interval = window.setInterval(
      refreshClock,
      period === 'day' ? DAILY_REFRESH_MS : INTRADAY_REFRESH_MS,
    )
    const refreshWhenVisible = () => {
      if (document.visibilityState === 'visible') refreshClock()
    }
    window.addEventListener('focus', refreshClock)
    document.addEventListener('visibilitychange', refreshWhenVisible)
    return () => {
      window.clearInterval(interval)
      window.removeEventListener('focus', refreshClock)
      document.removeEventListener('visibilitychange', refreshWhenVisible)
    }
  }, [period])

  useEffect(() => {
    let active = true
    const cached = getKline(symbol, period, { range })
    setEntry(cached)
    setError(cached?.error || '')
    setNoMoreEarlier(false)
    setCoverageMessage('')
    setLoading(!cached?.bars.length)
    if (!initialChunk) {
      setLoading(false)
      return () => { active = false }
    }
    void (async () => {
      try {
        let current = await loadWindow(initialChunk)
        if (!active) return
        setEntry(current)
        setError(current.error || '')

        // “近 1 日 / 5 日”由实际有数据的交易日决定，必要时自动向前找齐。
        let attempts = 0
        let selected = trimBarsToRange(current.bars, plan)
        let state = klineCoverage(plan, current.requestedStart, selected)
        while (active && plan.tradingDays && !state.complete && attempts < 6) {
          const earlier = getKlineChunkWindow(period, plan, current.requestedStart)
          if (!earlier) break
          current = await loadWindow(earlier)
          attempts += 1
          selected = trimBarsToRange(current.bars, plan)
          state = klineCoverage(plan, current.requestedStart, selected)
          setEntry(current)
          setError(current.error || '')
        }
        if (active && plan.tradingDays && !state.complete) {
          setNoMoreEarlier(true)
          setCoverageMessage('上游未提供足够的有效交易日，已保留当前可用数据。')
        } else if (active && !plan.tradingDays && !state.complete && current.requestedStart && Date.parse(current.requestedStart) <= plan.startMs) {
          setNoMoreEarlier(true)
          setCoverageMessage('上游数据未覆盖所选区间起点，已保留当前可用数据并标记覆盖不完整。')
        }
      } catch (reason) {
        if (!active) return
        setError(reason instanceof Error ? reason.message : 'K 线加载失败')
      } finally {
        if (active) setLoading(false)
      }
    })()
    return () => { active = false }
  }, [getKline, initialChunk, loadWindow, period, plan, range, symbol])

  const loadEarlier = useCallback(async () => {
    if (loadingEarlier || coverage.complete || noMoreEarlier) return
    const earlier = getKlineChunkWindow(period, plan, entry?.requestedStart || initialChunk?.start)
    if (!earlier) {
      setNoMoreEarlier(true)
      return
    }
    const previousCount = entry?.bars.length || 0
    setLoadingEarlier(true)
    setCoverageMessage('')
    try {
      const next = await loadWindow(earlier)
      setEntry(next)
      setError(next.error || '')
      if (next.error || next.bars.length <= previousCount) {
        setNoMoreEarlier(true)
        setCoverageMessage(next.error ? `更早分段加载失败：${next.error}` : '上游没有返回更早的分钟线，当前覆盖区间不完整。')
      }
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : '更早 K 线加载失败'
      setNoMoreEarlier(true)
      setCoverageMessage(`更早分段加载失败：${message}`)
    } finally {
      setLoadingEarlier(false)
    }
  }, [coverage.complete, entry?.bars.length, entry?.requestedStart, initialChunk?.start, loadWindow, loadingEarlier, noMoreEarlier, period, plan])

  const stats = useMemo(() => {
    if (!quote) return []
    const amplitude = quote.high !== undefined && quote.low !== undefined && quote.prev_close
      ? ((quote.high - quote.low) / quote.prev_close) * 100 : undefined
    const spread = quote.high !== undefined && quote.low !== undefined ? quote.high - quote.low : undefined
    const fromOpen = quote.open ? ((quote.price / quote.open) - 1) * 100 : undefined
    return [
      { label: '今开', value: quote.open }, { label: '最高', value: quote.high }, { label: '最低', value: quote.low },
      { label: '昨收', value: quote.prev_close }, { label: '成交量', value: quote.volume, amount: true }, { label: '成交额', value: quote.amount, amount: true },
      { label: '振幅', value: amplitude, percent: true }, { label: '日内价差', value: spread }, { label: '开盘至今', value: fromOpen, percent: true },
    ]
  }, [quote])

  function locate() {
    const value = search.trim().toUpperCase()
    if (!value) return
    const normalized = /\.(SH|SZ|BJ)$/.test(value) ? value : `${value}.${value.startsWith('6') ? 'SH' : value.startsWith('8') || value.startsWith('4') ? 'BJ' : 'SZ'}`
    setSymbol(normalized)
    setSearch('')
  }

  const progress = Math.round(coverage.progress * 100)
  const targetDates = `${new Date(plan.start).toLocaleDateString('zh-CN', { timeZone: 'Asia/Shanghai' })} — ${new Date(plan.end).toLocaleDateString('zh-CN', { timeZone: 'Asia/Shanghai' })}`

  return <div className="market-layout">
    <MarketClosedNotice />
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
          <button className={isWatched ? 'secondary active' : 'secondary'} onClick={() => { void (isWatched ? removeWatch(symbol) : addWatch(symbol)).catch(() => undefined) }}>{isWatched ? '★ 已自选' : '☆ 加自选'}</button>
        </div>
        <div className="quote-stats">{stats.map((item) => <div key={item.label}><span>{item.label}</span><strong>{item.amount ? formatAmount(item.value) : `${formatNumber(item.value)}${item.percent && item.value !== undefined ? '%' : ''}`}</strong></div>)}</div>
      </Panel>
      <Panel title="价格与技术指标" eyebrow="ADJUSTED KLINE">
        <div className="kline-selectors">
          <div><span>采样周期</span><div className="period-tabs">{KLINE_PERIODS.map((item) => <button key={item.value} className={period === item.value ? 'active' : ''} onClick={() => setPeriod(item.value)}>{item.label}</button>)}</div></div>
          <div><span>查看区间</span><div className="period-tabs range-tabs">{KLINE_RANGES.map((item) => <button key={item.value} className={range === item.value ? 'active' : ''} onClick={() => setRange(item.value)}>{item.label}</button>)}</div></div>
        </div>
        <div className="data-meta"><span><i className={(entry?.status?.delayed ?? delayed) ? 'warn' : ''} />{entry?.status?.source || source}{(entry?.status?.delayed ?? delayed) ? ' · 非实时缓存' : ' · 实时'}</span><span>后复权</span><span>采集 {entry?.status?.collected_at || updatedAt ? new Date(entry?.status?.collected_at || updatedAt!).toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '—'}</span></div>
        <div className="coverage-status">
          <div><span>目标区间</span><strong>{plan.label}{plan.tradingDays ? `（${plan.tradingDays} 个有数据交易日）` : `（${targetDates}）`}</strong></div>
          <div><span>已加载区间</span><strong>{loadedRangeLabel(bars, period)}</strong></div>
          <div><span>加载进度</span><strong>{progress}%</strong></div>
          <div className="coverage-progress" aria-label={`加载进度 ${progress}%`}><i style={{ width: `${progress}%` }} /></div>
        </div>
        <ErrorNotice message={error} />
        {coverageMessage && <div className="kline-coverage-warning" role="status">{coverageMessage}</div>}
        {loading && !bars.length ? <Loading label="加载后复权序列" /> : <CandlestickChart key={`${symbol}:${period}:${range}`} bars={bars} period={period} hasEarlier={!coverage.complete && !noMoreEarlier} loadingEarlier={loadingEarlier} onLoadEarlier={loadEarlier} />}
      </Panel>
    </div>
  </div>
}
