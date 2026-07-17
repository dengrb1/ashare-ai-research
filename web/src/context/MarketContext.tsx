import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { api, unwrapEnvelope } from '../api'
import { DEFAULT_WATCHLIST, marketPrefetchSymbols } from '../marketAssets'
import type { KlineBar, KlinePayload, MarketDataStatus, Quote } from '../types'

const PREFETCH_INTERVAL_MS = 5 * 60 * 1000
const DAILY_CACHE_MS = 5 * 60 * 1000
const INTRADAY_CACHE_MS = 30 * 1000

export interface KlineCacheEntry {
  symbol: string
  period: string
  bars: KlineBar[]
  status: MarketDataStatus | null
  fetchedAt: number
  stale: boolean
  error?: string
}

interface MarketValue {
  quotes: Record<string, Quote>
  watchlist: string[]
  delayed: boolean
  source: string
  updatedAt?: string
  error?: string
  klineVersion: number
  addWatch: (symbol: string) => void
  removeWatch: (symbol: string) => void
  subscribe: (symbols: string[]) => () => void
  refresh: () => Promise<void>
  prefetch: () => Promise<void>
  getKline: (symbol: string, period: string, limit?: number) => KlineCacheEntry | undefined
  loadKline: (symbol: string, period: string, limit?: number) => Promise<KlineCacheEntry>
}

const MarketContext = createContext<MarketValue | null>(null)

function savedWatchlist() {
  try {
    const value = JSON.parse(localStorage.getItem('ashare-watchlist') || 'null')
    return Array.isArray(value) ? value : DEFAULT_WATCHLIST
  } catch {
    return DEFAULT_WATCHLIST
  }
}

function klineKey(symbol: string, period: string, limit: number) {
  return `${symbol.toUpperCase()}:${period.toLowerCase()}:${limit}`
}

function cacheLifetime(period: string) {
  return period.toLowerCase() === 'day' ? DAILY_CACHE_MS : INTRADAY_CACHE_MS
}

function entryFromPayload(payload: KlinePayload, fallbackPeriod: string, now = Date.now()): KlineCacheEntry {
  return {
    symbol: payload.symbol,
    period: payload.period || fallbackPeriod,
    bars: (payload.bars || []).map((bar) => ({ ...bar, time: bar.time || bar.timestamp })),
    status: payload.status || null,
    fetchedAt: payload.status?.cached_at ? Date.parse(payload.status.cached_at) || now : now,
    stale: false,
  }
}

export function MarketProvider({ children }: { children: ReactNode }) {
  const [watchlist, setWatchlist] = useState<string[]>(savedWatchlist)
  const [quotes, setQuotes] = useState<Record<string, Quote>>({})
  const [meta, setMeta] = useState<{ delayed: boolean; source: string; updatedAt?: string; error?: string }>({ delayed: false, source: 'AKShare' })
  const subscribers = useRef(new Map<string, number>())
  const [subscriptionVersion, setSubscriptionVersion] = useState(0)
  const klineCache = useRef(new Map<string, KlineCacheEntry>())
  const klineInflight = useRef(new Map<string, Promise<KlineCacheEntry>>())
  const prefetchInflight = useRef<Promise<void> | null>(null)
  const [klineVersion, setKlineVersion] = useState(0)
  const activeSymbols = useMemo(() => Array.from(new Set([...watchlist, ...subscribers.current.keys()])).sort(), [watchlist, subscriptionVersion])
  const prefetchSymbols = useMemo(() => marketPrefetchSymbols(watchlist), [watchlist])

  const mergeQuotes = useCallback((rows: Quote[]) => {
    setQuotes((current) => {
      const next = { ...current }
      rows.forEach((quote) => { next[quote.symbol] = quote })
      return next
    })
    if (rows.length) {
      setMeta({
        delayed: rows.some((row) => Boolean(row.delayed || row.is_delayed || row.status?.delayed)),
        source: rows[0].source || rows[0].status?.source || 'AKShare',
        updatedAt: rows[0].cached_at || rows[0].status?.cached_at || new Date().toISOString(),
      })
    }
  }, [])

  const refresh = useCallback(async () => {
    if (!activeSymbols.length) return
    try {
      const rows = await api.quotes(activeSymbols)
      mergeQuotes(rows)
    } catch (error) {
      setMeta((current) => ({ ...current, delayed: true, error: error instanceof Error ? error.message : '行情服务暂不可用' }))
    }
  }, [activeSymbols.join(','), mergeQuotes])

  const prefetch = useCallback(async () => {
    if (!prefetchSymbols.length) return
    if (prefetchInflight.current) return prefetchInflight.current
    const work = (async () => {
      try {
        const payload = await api.prefetchMarket(prefetchSymbols, ['day'], 160)
        mergeQuotes(payload.quotes || [])
        let changed = false
        Object.entries(payload.klines || {}).forEach(([symbol, periods]) => {
          const daily = periods.day
          if (!daily) return
          klineCache.current.set(klineKey(symbol, 'day', 160), entryFromPayload(daily, 'day'))
          changed = true
        })
        if (changed) setKlineVersion((value) => value + 1)
        if (Object.keys(payload.errors || {}).length) {
          setMeta((current) => ({ ...current, error: `部分行情预加载失败：${Object.keys(payload.errors).join('、')}` }))
        }
      } catch (error) {
        setMeta((current) => ({ ...current, error: error instanceof Error ? error.message : '行情预加载失败' }))
      }
    })().finally(() => { prefetchInflight.current = null })
    prefetchInflight.current = work
    return work
  }, [prefetchSymbols.join(','), mergeQuotes])

  useEffect(() => {
    void refresh()
    if (!activeSymbols.length) return
    const timer = window.setInterval(() => void refresh(), 15_000)
    return () => window.clearInterval(timer)
  }, [refresh, activeSymbols.length])

  useEffect(() => {
    void prefetch()
    const timer = window.setInterval(() => void prefetch(), PREFETCH_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [prefetch])

  const subscribe = useCallback((symbols: string[]) => {
    const normalized = symbols.filter(Boolean)
    normalized.forEach((symbol) => subscribers.current.set(symbol, (subscribers.current.get(symbol) || 0) + 1))
    setSubscriptionVersion((value) => value + 1)
    return () => {
      normalized.forEach((symbol) => {
        const count = subscribers.current.get(symbol) || 0
        if (count <= 1) subscribers.current.delete(symbol)
        else subscribers.current.set(symbol, count - 1)
      })
      setSubscriptionVersion((value) => value + 1)
    }
  }, [])

  const updateWatchlist = useCallback((updater: (items: string[]) => string[]) => {
    setWatchlist((current) => {
      const next = updater(current)
      localStorage.setItem('ashare-watchlist', JSON.stringify(next))
      return next
    })
  }, [])
  const addWatch = useCallback((symbol: string) => updateWatchlist((items) => Array.from(new Set([...items, symbol.toUpperCase()]))), [updateWatchlist])
  const removeWatch = useCallback((symbol: string) => updateWatchlist((items) => items.filter((item) => item !== symbol)), [updateWatchlist])

  const getKline = useCallback((symbol: string, period: string, limit = 160) => {
    const entry = klineCache.current.get(klineKey(symbol, period, limit))
    if (!entry) return undefined
    return { ...entry, stale: entry.stale || Date.now() - entry.fetchedAt >= cacheLifetime(period) }
  }, [])

  const loadKline = useCallback(async (symbol: string, period: string, limit = 160) => {
    const normalized = symbol.toUpperCase()
    const key = klineKey(normalized, period, limit)
    const cached = klineCache.current.get(key)
    if (cached && Date.now() - cached.fetchedAt < cacheLifetime(period)) return { ...cached }
    const existing = klineInflight.current.get(key)
    if (existing) return existing
    const work = api.kline(normalized, period, limit).then((payload) => {
      const { data, meta } = unwrapEnvelope(payload, ['bars', 'items', 'data'])
      const entry: KlineCacheEntry = {
        symbol: normalized,
        period,
        bars: Array.isArray(data) ? data : [],
        status: meta.status || null,
        fetchedAt: Date.now(),
        stale: false,
      }
      klineCache.current.set(key, entry)
      setKlineVersion((value) => value + 1)
      return entry
    }).catch((reason) => {
      const message = reason instanceof Error ? reason.message : 'K 线加载失败'
      if (cached) {
        const fallback = { ...cached, fetchedAt: Date.now(), stale: true, error: message }
        klineCache.current.set(key, fallback)
        setKlineVersion((value) => value + 1)
        return fallback
      }
      throw reason
    }).finally(() => { klineInflight.current.delete(key) })
    klineInflight.current.set(key, work)
    return work
  }, [])

  const value = useMemo(() => ({ quotes, watchlist, ...meta, klineVersion, addWatch, removeWatch, subscribe, refresh, prefetch, getKline, loadKline }), [quotes, watchlist, meta, klineVersion, addWatch, removeWatch, subscribe, refresh, prefetch, getKline, loadKline])
  return <MarketContext.Provider value={value}>{children}</MarketContext.Provider>
}

export function useMarket() {
  const value = useContext(MarketContext)
  if (!value) throw new Error('useMarket must be used within MarketProvider')
  return value
}

export function useQuoteSubscription(symbols: string[]) {
  const { subscribe } = useMarket()
  const key = [...symbols].sort().join(',')
  useEffect(() => subscribe(key ? key.split(',') : []), [subscribe, key])
}
