import { useSyncExternalStore } from 'react'
import type { Quote } from '../types'

export type StreamConnectionState = 'CONNECTED' | 'RECONNECTING' | 'DISCONNECTED'

class MarketQuoteStore {
  private quotes = new Map<string, Quote>()
  private pending = new Map<string, Quote>()
  private listeners = new Map<string, Set<() => void>>()
  private globalListeners = new Set<() => void>()
  private flushTimer: number | null = null
  streamSnapshot = { state: 'CONNECTED' as StreamConnectionState, source: '实时行情', reconnectAttempt: 0 }
  connectionState: StreamConnectionState = 'CONNECTED'
  sourceName = '实时行情'
  reconnectAttempt = 0

  ingestBatch(items: Quote[], source?: string) {
    for (const item of items) if (item?.symbol) this.pending.set(item.symbol, item)
    if (source) this.sourceName = source
    this.streamSnapshot = { state: this.connectionState, source: this.sourceName, reconnectAttempt: this.reconnectAttempt }
    this.setConnectionState('CONNECTED', 0)
    if (this.flushTimer === null) {
      this.flushTimer = window.setTimeout(() => {
        this.flushTimer = null
        this.flushPending()
      }, 150)
    }
  }

  private flushPending() {
    const dirty: string[] = []
    for (const [symbol, next] of this.pending) {
      const previous = this.quotes.get(symbol)
      if (!previous || previous.price !== next.price || previous.change_pct !== next.change_pct || previous.volume !== next.volume || previous.turnover_rate !== next.turnover_rate) {
        this.quotes.set(symbol, next)
        dirty.push(symbol)
      }
    }
    this.pending.clear()
    dirty.forEach((symbol) => this.listeners.get(symbol)?.forEach((listener) => listener()))
  }

  setConnectionState(next: StreamConnectionState, attempt = 0) {
    if (this.connectionState === next && this.reconnectAttempt === attempt) return
    this.connectionState = next
    this.reconnectAttempt = attempt
    this.streamSnapshot = { state: next, source: this.sourceName, reconnectAttempt: attempt }
    this.globalListeners.forEach((listener) => listener())
  }

  getQuote = (symbol: string) => this.quotes.get(symbol)

  subscribeSymbol = (symbol: string) => (listener: () => void) => {
    let bucket = this.listeners.get(symbol)
    if (!bucket) {
      bucket = new Set()
      this.listeners.set(symbol, bucket)
    }
    bucket.add(listener)
    return () => {
      bucket?.delete(listener)
      if (bucket?.size === 0) this.listeners.delete(symbol)
    }
  }

  subscribeGlobal = (listener: () => void) => {
    this.globalListeners.add(listener)
    return () => this.globalListeners.delete(listener)
  }
}

export const marketQuoteStore = new MarketQuoteStore()

export function useSymbolQuote(symbol: string): Quote | undefined {
  return useSyncExternalStore(marketQuoteStore.subscribeSymbol(symbol), () => marketQuoteStore.getQuote(symbol))
}

export function useQuoteStreamState() {
  return useSyncExternalStore(marketQuoteStore.subscribeGlobal, () => marketQuoteStore.streamSnapshot)
}
