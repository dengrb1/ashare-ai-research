import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { api, unwrapEnvelope } from '../api'
import { marketPrefetchSymbols } from '../marketAssets'
import { mergeKlineBars } from '../marketKlines'
import type { KlineBar, KlineLoadOptions, KlinePayload, KlineRange, MarketDataStatus, MarketSessionStatus, PaperPosition, Quote } from '../types'

const PREFETCH_INTERVAL_MS = 5 * 60 * 1000
const DAILY_CACHE_MS = 5 * 60 * 1000
const INTRADAY_CACHE_MS = 30 * 1000
const MARKET_STATUS_POLL_MS = 60 * 1000
const VISIBILITY_REFRESH_THROTTLE_MS = 5 * 1000
export const MARKET_REFRESH_INTERVAL_OPTIONS = [5, 10, 15, 30, 60, 120] as const
type MarketRefreshIntervalSeconds = (typeof MARKET_REFRESH_INTERVAL_OPTIONS)[number]

export interface KlineCacheEntry {
  symbol: string
  period: string
  adjustment?: string
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
  exitMonitorEnabled: boolean
  defaultProfitTrigger: number | null
  stopLossMonitorEnabled: boolean
  buyMonitorEnabled: boolean
  marketRefreshIntervalSeconds: MarketRefreshIntervalSeconds
  assetsLoading: boolean
  assetsSaving: boolean
  quotesLoading: boolean
  delayed: boolean
  source: string
  updatedAt?: string
  error?: string
  marketSession: MarketSessionStatus | null
  klineVersion: number
  addWatch: (symbol: string) => Promise<void>
  removeWatch: (symbol: string) => Promise<void>
  reorderWatchlist: (nextOrder: string[]) => Promise<void>
  upsertPosition: (position: PaperPosition, previousSymbol?: string) => Promise<void>
  removePosition: (symbol: string) => Promise<void>
  saveTotalAssets: (totalAssets: number | null) => Promise<void>
  saveExitSettings: (enabled: boolean, amount: number | null, stopLossEnabled?: boolean, buyMonitorEnabled?: boolean) => Promise<void>
  saveMarketRefreshInterval: (seconds: MarketRefreshIntervalSeconds) => Promise<void>
  subscribe: (symbols: string[]) => () => void
  refresh: (force?: boolean) => Promise<void>
  refreshSymbol: (symbol: string, force?: boolean) => Promise<void>
  refreshRemaining: (excludedSymbol?: string, force?: boolean) => Promise<void>
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
    adjustment: payload.adjustment,
    bars: (payload.bars || []).map((bar) => ({ ...bar, time: bar.time || bar.timestamp })),
    status: payload.status || null,
    fetchedAt: payload.status?.cached_at ? Date.parse(payload.status.cached_at) || now : now,
    stale: false,
  }
}

function scheduleWhenIdle(callback: () => void) {
  const browser = window as Window & {
    requestIdleCallback?: (work: () => void, options?: { timeout: number }) => number
    cancelIdleCallback?: (handle: number) => void
  }
  if (typeof browser.requestIdleCallback === 'function') {
    const handle = browser.requestIdleCallback(callback, { timeout: 1_500 })
    return () => browser.cancelIdleCallback?.(handle)
  }
  const handle = window.setTimeout(callback, 0)
  return () => window.clearTimeout(handle)
}

export function MarketProvider({ children }: { children: ReactNode }) {
  const [watchlist, setWatchlist] = useState<string[]>([])
  const [positions, setPositions] = useState<PaperPosition[]>([])
  const [totalAssets, setTotalAssets] = useState<number | null>(null)
  const [exitMonitorEnabled, setExitMonitorEnabled] = useState(false)
  const [defaultProfitTrigger, setDefaultProfitTrigger] = useState<number | null>(null)
  const [stopLossMonitorEnabled, setStopLossMonitorEnabled] = useState(true)
  const [buyMonitorEnabled, setBuyMonitorEnabled] = useState(true)
  const [marketRefreshIntervalSeconds, setMarketRefreshIntervalSeconds] = useState<MarketRefreshIntervalSeconds>(5)
  const [assetsLoading, setAssetsLoading] = useState(true)
  const [assetsSaving, setAssetsSaving] = useState(false)
  const [quotesLoading, setQuotesLoading] = useState(true)
  const [quotes, setQuotes] = useState<Record<string, Quote>>({})
  const [marketSession, setMarketSession] = useState<MarketSessionStatus | null>(null)
  const [meta, setMeta] = useState<{ delayed: boolean; source: string; updatedAt?: string; error?: string }>({ delayed: false, source: 'AKShare' })
  const subscribers = useRef(new Map<string, number>())
  const [subscriptionVersion, setSubscriptionVersion] = useState(0)
  const klineCache = useRef(new Map<string, KlineCacheEntry>())
  const klineSeriesCache = useRef(new Map<string, KlineCacheEntry>())
  const klineInflight = useRef(new Map<string, Promise<KlineCacheEntry>>())
  const assetMutation = useRef(false)
  const prefetchInflight = useRef<Promise<void> | null>(null)
  const prefetchController = useRef<AbortController | null>(null)
  const focusedQuoteInflight = useRef(new Map<string, Promise<void>>())
  const focusedQuoteControllers = useRef(new Map<string, AbortController>())
  const bulkQuoteInflight = useRef<Promise<void> | null>(null)
  const bulkQuoteController = useRef<AbortController | null>(null)
  const refreshQueue = useRef<Promise<void> | null>(null)
  const lastVisibilityRefreshAt = useRef(0)
  const quoteLoadCount = useRef(0)
  const [klineVersion, setKlineVersion] = useState(0)
  const prefetchSymbols = useMemo(() => marketPrefetchSymbols(watchlist, positions), [watchlist, positions])
  const activeSymbols = useMemo(
    () => Array.from(new Set([...subscribers.current.keys(), ...prefetchSymbols])),
    [prefetchSymbols, subscriptionVersion],
  )

  useEffect(() => {
    let active = true
    void api.assets().then((state) => {
      if (!active) return
      setWatchlist(state.watchlist || [])
      setPositions(state.positions || [])
      setTotalAssets(state.total_assets ?? null)
      setExitMonitorEnabled(Boolean(state.exit_monitor_enabled))
      setDefaultProfitTrigger(state.default_profit_trigger == null ? null : Number(state.default_profit_trigger))
      setStopLossMonitorEnabled(state.stop_loss_monitor_enabled !== false)
      setBuyMonitorEnabled(state.buy_monitor_enabled !== false)
      setMarketRefreshIntervalSeconds(state.market_refresh_interval_seconds ?? 5)
    }).catch((error) => {
      if (!active) return
      setMeta((current) => ({ ...current, error: error instanceof Error ? error.message : '自选与持仓加载失败' }))
    }).finally(() => { if (active) setAssetsLoading(false) })
    return () => { active = false }
  }, [])

  useEffect(() => {
    let active = true
    const refreshMarketSession = () => {
      void api.marketStatus().then((status) => {
        if (active) setMarketSession(status.market_session || null)
      }).catch(() => {
        if (active) setMarketSession(null)
      })
    }
    const refreshWhenVisible = () => {
      if (document.visibilityState === 'visible') refreshMarketSession()
    }
    refreshMarketSession()
    const timer = window.setInterval(refreshMarketSession, MARKET_STATUS_POLL_MS)
    window.addEventListener('focus', refreshWhenVisible)
    document.addEventListener('visibilitychange', refreshWhenVisible)
    return () => {
      active = false
      window.clearInterval(timer)
      window.removeEventListener('focus', refreshWhenVisible)
      document.removeEventListener('visibilitychange', refreshWhenVisible)
    }
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

  const beginQuoteLoad = useCallback(() => {
    quoteLoadCount.current += 1
    setQuotesLoading(true)
  }, [])

  const endQuoteLoad = useCallback(() => {
    quoteLoadCount.current = Math.max(0, quoteLoadCount.current - 1)
    if (quoteLoadCount.current === 0) setQuotesLoading(false)
  }, [])

  const reportQuoteError = useCallback((error: unknown) => {
    setMeta((current) => ({
      ...current,
      delayed: true,
      error: error instanceof Error ? error.message : '行情服务暂不可用',
    }))
  }, [])

  const refreshSymbol = useCallback(async (symbol: string, force = false) => {
    const normalized = symbol.trim().toUpperCase()
    if (!normalized) return
    const key = normalized
    const existing = focusedQuoteInflight.current.get(key)
    if (existing && !force) return existing
    if (existing && force) focusedQuoteControllers.current.get(key)?.abort()
    const controller = new AbortController()
    focusedQuoteControllers.current.set(key, controller)
    beginQuoteLoad()
    let work: Promise<void>
    work = api.quote(normalized, force, controller.signal)
      .then((row) => { mergeQuotes([row]) })
      .catch((error) => {
        if (error instanceof DOMException && error.name === 'AbortError') return
        reportQuoteError(error)
      })
      .finally(() => {
        if (focusedQuoteInflight.current.get(key) === work) {
          focusedQuoteInflight.current.delete(key)
        }
        if (focusedQuoteControllers.current.get(key) === controller) {
          focusedQuoteControllers.current.delete(key)
        }
        endQuoteLoad()
      })
    focusedQuoteInflight.current.set(key, work)
    return work
  }, [beginQuoteLoad, endQuoteLoad, mergeQuotes, reportQuoteError])

  const refreshRemaining = useCallback(async (excludedSymbol?: string, force = false) => {
    const excluded = excludedSymbol?.trim().toUpperCase()
    const symbols = activeSymbols.filter((symbol) => symbol !== excluded)
    if (!symbols.length) return
    if (bulkQuoteInflight.current && !force) return bulkQuoteInflight.current
    if (bulkQuoteInflight.current && force) bulkQuoteController.current?.abort()
    const controller = new AbortController()
    bulkQuoteController.current = controller
    beginQuoteLoad()
    let work: Promise<void>
    work = api.quotes(symbols, force, controller.signal)
      .then(mergeQuotes)
      .catch((error) => {
        if (error instanceof DOMException && error.name === 'AbortError') return
        reportQuoteError(error)
      })
      .finally(() => {
        if (bulkQuoteInflight.current === work) {
          bulkQuoteInflight.current = null
        }
        if (bulkQuoteController.current === controller) {
          bulkQuoteController.current = null
        }
        endQuoteLoad()
      })
    bulkQuoteInflight.current = work
    return work
  }, [activeSymbols.join(','), beginQuoteLoad, endQuoteLoad, mergeQuotes, reportQuoteError])

  const refresh = useCallback(async (force = false) => {
    if (refreshQueue.current) return refreshQueue.current
    if (!activeSymbols.length) {
      setQuotesLoading(false)
      return
    }
    const work = (async () => {
      const prioritySymbol = activeSymbols[0]
      await refreshSymbol(prioritySymbol, force)
      await refreshRemaining(prioritySymbol, force)
    })().finally(() => {
      refreshQueue.current = null
    })
    refreshQueue.current = work
    return work
  }, [activeSymbols.join(','), refreshRemaining, refreshSymbol])

  const prefetch = useCallback(async () => {
    if (!prefetchSymbols.length) return
    if (prefetchInflight.current) return prefetchInflight.current
    const controller = new AbortController()
    prefetchController.current = controller
    let work: Promise<void>
    work = (async () => {
      try {
        const payload = await api.prefetchMarket(prefetchSymbols, ['day'], 160, false, controller.signal)
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
        if (error instanceof DOMException && error.name === 'AbortError') return
        setMeta((current) => ({ ...current, error: error instanceof Error ? error.message : '行情预加载失败' }))
      }
    })().finally(() => {
      if (prefetchInflight.current === work) {
        prefetchInflight.current = null
      }
      if (prefetchController.current === controller) {
        prefetchController.current = null
      }
    })
    prefetchInflight.current = work
    return work
  }, [prefetchSymbols.join(',')])

  useEffect(() => {
    if (assetsLoading) return
    let active = true
    let cancelIdle = () => {}
    void refresh().then(() => {
      if (!active) return
      cancelIdle = scheduleWhenIdle(() => {
        if (active) void prefetch()
      })
    })
    const timer = window.setInterval(() => void refresh(), marketRefreshIntervalSeconds * 1_000)
    return () => {
      active = false
      cancelIdle()
      window.clearInterval(timer)
      focusedQuoteControllers.current.forEach((controller) => controller.abort())
      bulkQuoteController.current?.abort()
      prefetchController.current?.abort()
    }
  }, [assetsLoading, marketRefreshIntervalSeconds, prefetch, refresh])

  useEffect(() => {
    if (assetsLoading || !prefetchSymbols.length) return
    const timer = window.setInterval(() => void prefetch(), PREFETCH_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [assetsLoading, prefetch, prefetchSymbols.length])

  useEffect(() => {
    if (assetsLoading) return
    const refreshWhenVisible = () => {
      if (document.visibilityState !== 'visible') return
      const now = Date.now()
      if (now - lastVisibilityRefreshAt.current < VISIBILITY_REFRESH_THROTTLE_MS) return
      lastVisibilityRefreshAt.current = now
      // Browsers throttle background timers.  A forced quote read on return
      // prevents an old in-memory or Redis entry from surviving a tab switch.
      void refresh(true)
      void prefetch()
    }
    window.addEventListener('focus', refreshWhenVisible)
    document.addEventListener('visibilitychange', refreshWhenVisible)
    return () => {
      window.removeEventListener('focus', refreshWhenVisible)
      document.removeEventListener('visibilitychange', refreshWhenVisible)
    }
  }, [assetsLoading, prefetch, refresh])

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

  const saveAssets = useCallback(async (nextWatchlist: string[], nextPositions: PaperPosition[], nextTotalAssets = totalAssets, nextExitEnabled = exitMonitorEnabled, nextProfitTrigger = defaultProfitTrigger, nextStopLossEnabled = stopLossMonitorEnabled, nextBuyMonitorEnabled = buyMonitorEnabled) => {
    try {
      const saved = await api.saveAssets({ watchlist: nextWatchlist, positions: nextPositions, total_assets: nextTotalAssets, exit_monitor_enabled: nextExitEnabled, default_profit_trigger: nextProfitTrigger, stop_loss_monitor_enabled: nextStopLossEnabled, buy_monitor_enabled: nextBuyMonitorEnabled, market_refresh_interval_seconds: marketRefreshIntervalSeconds })
      setWatchlist(saved.watchlist)
      setPositions(saved.positions)
      setTotalAssets(saved.total_assets ?? null)
      setExitMonitorEnabled(Boolean(saved.exit_monitor_enabled))
      setDefaultProfitTrigger(saved.default_profit_trigger == null ? null : Number(saved.default_profit_trigger))
      setStopLossMonitorEnabled(saved.stop_loss_monitor_enabled !== false)
      setBuyMonitorEnabled(saved.buy_monitor_enabled !== false)
      setMarketRefreshIntervalSeconds(saved.market_refresh_interval_seconds ?? 5)
    } catch (error) {
      setMeta((current) => ({ ...current, error: error instanceof Error ? error.message : '自选与持仓保存失败' }))
      throw error
    }
  }, [buyMonitorEnabled, defaultProfitTrigger, exitMonitorEnabled, marketRefreshIntervalSeconds, stopLossMonitorEnabled, totalAssets])

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

  const saveExitSettings = useCallback(async (enabled: boolean, amount: number | null, nextStopLossEnabled = stopLossMonitorEnabled, nextBuyMonitorEnabled = buyMonitorEnabled) => {
    beginAssetMutation(); const previousEnabled = exitMonitorEnabled; const previousAmount = defaultProfitTrigger; const previousStopLoss = stopLossMonitorEnabled; const previousBuyMonitor = buyMonitorEnabled
    setExitMonitorEnabled(enabled); setDefaultProfitTrigger(amount); setStopLossMonitorEnabled(nextStopLossEnabled); setBuyMonitorEnabled(nextBuyMonitorEnabled)
    try {
      const saved = await api.saveExitMonitorSettings({
        exit_monitor_enabled: enabled,
        default_profit_trigger: amount,
        stop_loss_monitor_enabled: nextStopLossEnabled,
        buy_monitor_enabled: nextBuyMonitorEnabled,
      })
      const savedTrigger = Number(saved.default_profit_trigger)
      setExitMonitorEnabled(Boolean(saved.exit_monitor_enabled))
      setDefaultProfitTrigger(
        saved.default_profit_trigger == null || !Number.isFinite(savedTrigger) ? null : savedTrigger,
      )
      setStopLossMonitorEnabled(saved.stop_loss_monitor_enabled ?? true)
      setBuyMonitorEnabled(saved.buy_monitor_enabled ?? true)
      setMarketRefreshIntervalSeconds(saved.market_refresh_interval_seconds ?? 5)
    }
    catch (error) { setExitMonitorEnabled(previousEnabled); setDefaultProfitTrigger(previousAmount); setStopLossMonitorEnabled(previousStopLoss); setBuyMonitorEnabled(previousBuyMonitor); throw error }
    finally { endAssetMutation() }
  }, [beginAssetMutation, buyMonitorEnabled, defaultProfitTrigger, endAssetMutation, exitMonitorEnabled, stopLossMonitorEnabled])

  const saveMarketRefreshInterval = useCallback(async (seconds: MarketRefreshIntervalSeconds) => {
    if (!MARKET_REFRESH_INTERVAL_OPTIONS.includes(seconds)) {
      throw new Error('不支持的自动刷新间隔')
    }
    beginAssetMutation()
    const previous = marketRefreshIntervalSeconds
    setMarketRefreshIntervalSeconds(seconds)
    try {
      const saved = await api.saveMarketRefreshSettings(seconds)
      setMarketRefreshIntervalSeconds(saved.market_refresh_interval_seconds ?? 5)
    } catch (error) {
      setMarketRefreshIntervalSeconds(previous)
      setMeta((current) => ({ ...current, error: error instanceof Error ? error.message : '自动刷新设置保存失败' }))
      throw error
    } finally {
      endAssetMutation()
    }
  }, [beginAssetMutation, endAssetMutation, marketRefreshIntervalSeconds])

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
      const cachedChunkIsFresh = cachedChunk
        && Date.now() - cachedChunk.fetchedAt < cacheLifetime(period)
      if (!request.refresh && cachedChunkIsFresh) return mergeChunk(cachedChunk)
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

  const value = useMemo(() => ({ quotes, watchlist, positions, totalAssets, exitMonitorEnabled, defaultProfitTrigger, stopLossMonitorEnabled, buyMonitorEnabled, marketRefreshIntervalSeconds, assetsLoading, assetsSaving, quotesLoading, marketSession, ...meta, klineVersion, addWatch, removeWatch, reorderWatchlist, upsertPosition, removePosition, saveTotalAssets, saveExitSettings, saveMarketRefreshInterval, subscribe, refresh, refreshSymbol, refreshRemaining, prefetch, getKline, loadKline }), [quotes, watchlist, positions, totalAssets, exitMonitorEnabled, defaultProfitTrigger, stopLossMonitorEnabled, buyMonitorEnabled, marketRefreshIntervalSeconds, assetsLoading, assetsSaving, quotesLoading, marketSession, meta, klineVersion, addWatch, removeWatch, reorderWatchlist, upsertPosition, removePosition, saveTotalAssets, saveExitSettings, saveMarketRefreshInterval, subscribe, refresh, refreshSymbol, refreshRemaining, prefetch, getKline, loadKline])
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
