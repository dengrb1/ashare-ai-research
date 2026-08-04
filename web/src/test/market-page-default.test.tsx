import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { KlineCacheEntry } from '../context/MarketContext'

const { state } = vi.hoisted(() => ({
  state: {
    watchlist: [] as string[],
    quotes: {} as Record<string, { name: string; price: number; change_pct: number; symbol: string }>,
  },
}))

const EMPTY_KLINE: KlineCacheEntry = { symbol: '', period: 'day', bars: [], status: null, fetchedAt: 0, stale: false }

vi.mock('../context/MarketContext', () => ({
  useMarket: () => ({
    watchlist: state.watchlist,
    quotes: state.quotes,
    addWatch: vi.fn(),
    removeWatch: vi.fn(),
    source: 'fixture',
    delayed: false,
    updatedAt: '2026-07-20T00:00:00Z',
    refreshSymbol: vi.fn(),
    refreshRemaining: vi.fn(),
    getKline: vi.fn(() => undefined),
    loadKline: vi.fn(async () => EMPTY_KLINE),
    marketSession: null,
  }),
  useQuoteSubscription: () => undefined,
}))

vi.mock('../context/RefreshContext', () => ({ usePageRefresh: () => undefined }))

import { MarketPage } from '../pages/MarketPage'

describe('MarketPage default symbol', () => {
  it('never falls back to a hardcoded stock while the persisted watchlist loads', async () => {
    // A fresh refresh mounts before /assets returns, so the watchlist is empty.
    state.watchlist = []
    state.quotes = {}
    const { rerender } = render(<MarketPage />)
    expect(screen.getByText('未选择证券')).toBeInTheDocument()
    expect(screen.queryByText('贵州茅台')).not.toBeInTheDocument()

    // Simulate the persisted watchlist arriving after mount.
    state.watchlist = ['000001.SZ']
    state.quotes = { '000001.SZ': { name: '平安银行', price: 10, change_pct: 0, symbol: '000001.SZ' } }
    rerender(<MarketPage />)

    // The chart adopts the first watchlist entry instead of 600519.
    await waitFor(() => expect(screen.getByRole('heading', { name: '平安银行' })).toBeInTheDocument())
    expect(screen.queryByText('贵州茅台')).not.toBeInTheDocument()
  })
})
