import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { api, unwrapEnvelope } from '../api'
import { marketPrefetchSymbols } from '../marketAssets'
import { mergeKlineBars } from '../marketKlines'
import type { KlineBar, KlineLoadOptions, KlinePayload, KlineRange, MarketDataStatus, PaperPosition, Quote } from '../types'

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
  range?: KlineRange
  requestedStart?: string
  requestedEnd?: string
  chunks?: Array<{ key: string; start: string; end: string }>
}

interface MarketValue {
  quotes: Record<string, Quote>
  watchlist: string[]
  positions: PaperPosition[]
  totalAssets: number | null
  assetsLoading: boolean
  assetsSaving: boolean
  delayed: boolean
  source: string
  updatedAt?: string
  error?: string
  klineVersion: number
  addWatch: (symbol: string) => Promise<void>
  removeWatch: (symbol: string) => Promise<void>
  reorderWatchlist: (nextOrder: string[]) => Promise<void>
  upsertPosition: (position: PaperPosition, previousSymbol?: string) => Promise<void>
  removePosition: (symbol: string) => Promise<void>
  saveTotalAssets: (totalAssets: number | null) => Promise<void>
  subscribe: (symbols: string[]) => () => void
  refresh: (force?: boolean) => Promise<void>
  prefetch: () => Promise<void>
  getKline: (symbol: string, period: string, request?: number | { range: KlineRange }) => KlineCacheEntry | undefined
  loadKline: (symbol: string, period: string, request?: number | KlineLoadOptions, force?: boolean) => Promise<KlineCacheEntry>
}

const MarketContext = createContext<MarketValue | null>(null)

function klineKey(symbol: string, period: string, limit: number) {
  return `${symbol.toUpperCase()}:${period.toLowerCase()}:${limit}`
}

function klineSeriesKey(symbol: string, period: string, range: KlineRange) {
  return `${symbol.toUpperCase()}:${period.toLowerCase()}:range:${range}`
}

function klineChunkKey(symbol: string, period: string, request: KlineLoadOptions) {
  return `${klineSeriesKey(symbol, period, request.range)}:chunk:${request.chunk || `${request.start || ''}|${request.end || ''}`}:${request.limit || 1200}`
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
  const [watchlist, setWatchlist] = useState<string[]>([])
  const [positions, setPositions] = useState<PaperPosition[]>([])
  const [totalAssets, setTotalAssets] = useState<number | null>(null)
  const [assetsLoading, setAssetsLoading] = useState(true)
  const [assetsSaving, setAssetsSaving] = useState(false)
  const [quotes, setQuotes] = useState<Record<string, Quote>>({})
  const [meta, setMeta] = useState<{ delayed: boolean; source: string; updatedAt?: string; error?: string }>({ delayed: false, source: 'AKShare' })
  const subscribers = useRef(new Map<string, number>())
  const [subscriptionVersion, setSubscriptionVersion] = useState(0)
  const klineCache = useRef(new Map<string, KlineCacheEntry>())
  const klineSeriesCache = useRef(new Map<string, KlineCacheEntry>())
  const klineInflight = useRef(new Map<string, Promise<KlineCacheEntry>>())
  const assetMutation = useRef(false)
  const prefetchInflight = useRef<Promise<void> | null>(null)
  const [klineVersion, setKlineVersion] = useState(0)
  const activeSymbols = useMemo(() => Array.from(new Set([...watchlist, ...subscribers.current.keys()])).sort(), [watchlist, subscriptionVersion])
  const prefetchSymbols = useMemo(() => marketPrefetchSymbols(watchlist, positions), [watchlist, positions])

  useEffect(() => {
    let active = true
    void api.assets().then((state) => {
      if (!active) return
      setWatchlist(state.watchlist || [])
      setPositions(state.positions || [])
      setTotalAssets(state.total_assets ?? null)
    }).catch((error) => {
      if (!active) return
      setMeta((current) => ({ ...current, error: error instanceof Error ? error.message : '自选与持仓加载失败' }))
    }).finally(() => { if (active) setAssetsLoading(false) })
    return () => { active = false }
  }, [])

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

  const refresh = useCallback(async (force = false) => {
    if (!activeSymbols.length) return
    try {
      const rows = await api.quotes(activeSymbols, force)
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

  const saveAssets = useCallback(async (nextWatchlist: string[], nextPositions: PaperPosition[], nextTotalAssets = totalAssets) => {
    try {
      const saved = await api.saveAssets({ watchlist: nextWatchlist, positions: nextPositions, total_assets: nextTotalAssets })
      setWatchlist(saved.watchlist)
      setPositions(saved.positions)
      setTotalAssets(saved.total_assets ?? null)
    } catch (error) {
      setMeta((current) => ({ ...current, error: error instanceof Error ? error.message : '自选与持仓保存失败' }))
      throw error
    }
  }, [totalAssets])

  const beginAssetMutation = useCallback(() => {
    if (assetMutation.current) throw new Error('自选与持仓正在保存，请稍候')
    assetMutation.current = true
    setAssetsSaving(true)
  }, [])

  const endAssetMutation = useCallback(() => {
    assetMutation.current = false
    setAssetsSaving(false)
  }, [])

  const addWatch = useCallback(async (symbol: string) => {
    const normalized = symbol.trim().toUpperCase()
    const next = Array.from(new Set([...watchlist, normalized]))
    if (next.length === watchlist.length) return
    beginAssetMutation()
    setWatchlist(next)
    try { await saveAssets(next, positions) } catch (error) { setWatchlist(watchlist); throw error } finally { endAssetMutation() }
  }, [beginAssetMutation, endAssetMutation, positions, saveAssets, watchlist])

  const removeWatch = useCallback(async (symbol: string) => {
    const next = watchlist.filter((item) => item !== symbol)
    if (next.length === watchlist.length) return
    beginAssetMutation()
    setWatchlist(next)
    try { await saveAssets(next, positions) } catch (error) { setWatchlist(watchlist); throw error } finally { endAssetMutation() }
  }, [beginAssetMutation, endAssetMutation, positions, saveAssets, watchlist])

  const reorderWatchlist = useCallback(async (nextOrder: string[]) => {
    const normalized = nextOrder.map((symbol) => symbol.trim().toUpperCase())
    if (normalized.length !== watchlist.length || new Set(normalized).size !== normalized.length || normalized.some((symbol) => !watchlist.includes(symbol))) {
      throw new Error('自选排序必须包含当前全部证券且不能重复')
    }
    if (normalized.every((symbol, index) => symbol === watchlist[index])) return
    beginAssetMutation()
    setWatchlist(normalized)
    try { await saveAssets(normalized, positions) } catch (error) { setWatchlist(watchlist); throw error } finally { endAssetMutation() }
  }, [beginAssetMutation, endAssetMutation, positions, saveAssets, watchlist])

  const upsertPosition = useCallback(async (position: PaperPosition, previousSymbol?: string) => {
    const normalized = { ...position, symbol: position.symbol.trim().toUpperCase(), name: position.name.trim() }
    const replaced = positions.filter((item) => item.symbol !== (previousSymbol || normalized.symbol))
    const next = [...replaced, normalized].sort((left, right) => left.symbol.localeCompare(right.symbol))
    beginAssetMutation()
    setPositions(next)
    try { await saveAssets(watchlist, next) } catch (error) { setPositions(positions); throw error } finally { endAssetMutation() }
  }, [beginAssetMutation, endAssetMutation, positions, saveAssets, watchlist])

  const removePosition = useCallback(async (symbol: string) => {
    const next = positions.filter((item) => item.symbol !== symbol)
    if (next.length === positions.length) return
    beginAssetMutation()
    setPositions(next)
    try { await saveAssets(watchlist, next) } catch (error) { setPositions(positions); throw error } finally { endAssetMutation() }
  }, [beginAssetMutation, endAssetMutation, positions, saveAssets, watchlist])

  const saveTotalAssets = useCallback(async (nextTotalAssets: number | null) => {
    beginAssetMutation()
    setTotalAssets(nextTotalAssets)
    try { await saveAssets(watchlist, positions, nextTotalAssets) } catch (error) { setTotalAssets(totalAssets); throw error } finally { endAssetMutation() }
  }, [beginAssetMutation, endAssetMutation, positions, saveAssets, totalAssets, watchlist])

  const getKline = useCallback((symbol: string, period: string, request: number | { range: KlineRange } = 160) => {
    const entry = typeof request === 'number'
      ? klineCache.current.get(klineKey(symbol, period, request))
      : klineSeriesCache.current.get(klineSeriesKey(symbol, period, request.range))
    if (!entry) return undefined
    return { ...entry, stale: entry.stale || Date.now() - entry.fetchedAt >= cacheLifetime(period) }
  }, [])

  const loadKline = useCallback(async (symbol: string, period: string, request: number | KlineLoadOptions = 160, force = false) => {
    const normalized = symbol.toUpperCase()
    if (typeof request !== 'number') {
      const limit = request.limit || 1200
      const seriesKey = klineSeriesKey(normalized, period, request.range)
      const chunkKey = klineChunkKey(normalized, period, request)
      const cachedChunk = klineCache.current.get(chunkKey)
      const mergeChunk = (chunk: KlineCacheEntry) => {
        const current = klineSeriesCache.current.get(seriesKey)
        const chunks = [...(current?.chunks || [])]
        const nextChunk = { key: chunkKey, start: request.start || '', end: request.end || '' }
        const chunkIndex = chunks.findIndex((item) => item.key === chunkKey)
        if (chunkIndex >= 0) chunks[chunkIndex] = nextChunk
        else chunks.push(nextChunk)
        chunks.sort((left, right) => Date.parse(left.start) - Date.parse(right.start))
        const entry: KlineCacheEntry = {
          symbol: normalized,
          period,
          range: request.range,
          bars: mergeKlineBars(current?.bars || [], chunk.bars),
          status: chunk.status || current?.status || null,
          fetchedAt: Math.max(current?.fetchedAt || 0, chunk.fetchedAt),
          stale: chunk.stale || Boolean(current?.stale),
          error: chunk.error,
          requestedStart: chunks[0]?.start,
          requestedEnd: chunks[chunks.length - 1]?.end,
          chunks,
        }
        klineSeriesCache.current.set(seriesKey, entry)
        return entry
      }
      if (!request.refresh && cachedChunk) return mergeChunk(cachedChunk)
      const existing = klineInflight.current.get(chunkKey)
      if (existing) return existing
      const work = api.kline(normalized, period, limit, request).then((payload) => {
        const { data, meta } = unwrapEnvelope(payload, ['bars', 'items', 'data'])
        const chunk: KlineCacheEntry = {
          symbol: normalized,
          period,
          range: request.range,
          bars: Array.isArray(data) ? data : [],
          status: meta.status || null,
          fetchedAt: Date.now(),
          stale: false,
        }
        klineCache.current.set(chunkKey, chunk)
        const entry = mergeChunk(chunk)
        setKlineVersion((value) => value + 1)
        return entry
      }).catch((reason) => {
        const message = reason instanceof Error ? reason.message : 'K 线加载失败'
        if (cachedChunk) {
          const fallback = { ...cachedChunk, stale: true, error: message }
          klineCache.current.set(chunkKey, fallback)
          const entry = mergeChunk(fallback)
          setKlineVersion((value) => value + 1)
          return entry
        }
        const current = klineSeriesCache.current.get(seriesKey)
        if (current) {
          const fallback = { ...current, stale: true, error: message }
          klineSeriesCache.current.set(seriesKey, fallback)
          setKlineVersion((value) => value + 1)
          return fallback
        }
        throw reason
      }).finally(() => { klineInflight.current.delete(chunkKey) })
      klineInflight.current.set(chunkKey, work)
      return work
    }
    const limit = request
    const key = klineKey(normalized, period, limit)
    const cached = klineCache.current.get(key)
    if (!force && cached && Date.now() - cached.fetchedAt < cacheLifetime(period)) return { ...cached }
    const existing = klineInflight.current.get(key)
    if (existing) return existing
    const work = api.kline(normalized, period, limit, force).then((payload) => {
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

  const value = useMemo(() => ({ quotes, watchlist, positions, totalAssets, assetsLoading, assetsSaving, ...meta, klineVersion, addWatch, removeWatch, reorderWatchlist, upsertPosition, removePosition, saveTotalAssets, subscribe, refresh, prefetch, getKline, loadKline }), [quotes, watchlist, positions, totalAssets, assetsLoading, assetsSaving, meta, klineVersion, addWatch, removeWatch, reorderWatchlist, upsertPosition, removePosition, saveTotalAssets, subscribe, refresh, prefetch, getKline, loadKline])
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
