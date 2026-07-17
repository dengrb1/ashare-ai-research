import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MarketProvider, useMarket } from '../context/MarketContext'
import { ThemeProvider, ThemeToggle, useTheme } from '../context/ThemeContext'

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

function ThemeProbe() {
  const { theme } = useTheme()
  return <><span data-testid="theme">{theme}</span><ThemeToggle /></>
}

function MarketProbe() {
  const { addWatch, loadKline, getKline, klineVersion } = useMarket()
  const [result, setResult] = React.useState('')
  const cached = getKline('600519.SH', 'day')
  return <div data-version={klineVersion}>
    <span data-testid="cached-bars">{cached?.bars.length || 0}</span>
    <button onClick={() => addWatch('600000.SH')}>添加自选</button>
    <button onClick={() => void loadKline('600519.SH', 'day').then((entry) => setResult(`${entry.bars.length}:${entry.error || 'ok'}`))}>加载日线</button>
    <span data-testid="load-result">{result}</span>
  </div>
}

describe('theme system', () => {
  beforeEach(() => {
    localStorage.clear()
    document.documentElement.removeAttribute('data-theme')
  })
  afterEach(cleanup)

  it('defaults to light and persists theme changes', async () => {
    render(<ThemeProvider><ThemeProbe /></ThemeProvider>)
    expect(screen.getByTestId('theme')).toHaveTextContent('light')
    expect(document.documentElement).toHaveAttribute('data-theme', 'light')
    await userEvent.click(screen.getByRole('button', { name: '切换深色主题' }))
    expect(screen.getByTestId('theme')).toHaveTextContent('dark')
    expect(localStorage.getItem('ashare-theme')).toBe('dark')
    expect(document.documentElement).toHaveAttribute('data-theme', 'dark')
  })

  it('restores a saved dark theme', () => {
    localStorage.setItem('ashare-theme', 'dark')
    render(<ThemeProvider><ThemeProbe /></ThemeProvider>)
    expect(screen.getByTestId('theme')).toHaveTextContent('dark')
  })
})

describe('market prefetch and client cache', () => {
  beforeEach(() => {
    localStorage.clear()
    document.cookie = 'ashare_csrf=csrf-value; path=/'
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    document.cookie = 'ashare_csrf=; Max-Age=0; path=/'
  })

  it('prefetches watchlist plus holdings and reruns after watchlist changes', async () => {
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/market/quotes')) return jsonResponse([])
      if (url.endsWith('/market/prefetch')) return jsonResponse({ quotes: [], klines: {}, errors: {} })
      throw new Error(`unexpected request ${url} ${init?.method}`)
    })
    vi.stubGlobal('fetch', mockFetch)
    render(<MarketProvider><MarketProbe /></MarketProvider>)

    await waitFor(() => expect(mockFetch.mock.calls.some(([url]) => String(url).endsWith('/market/prefetch'))).toBe(true))
    const first = mockFetch.mock.calls.find(([url]) => String(url).endsWith('/market/prefetch'))
    const firstBody = JSON.parse(String(first?.[1]?.body))
    expect(firstBody.symbols).toEqual(expect.arrayContaining(['600519.SH', '300750.SZ', '601318.SH']))

    await userEvent.click(screen.getByRole('button', { name: '添加自选' }))
    await waitFor(() => {
      const bodies = mockFetch.mock.calls
        .filter(([url]) => String(url).endsWith('/market/prefetch'))
        .map(([, init]) => JSON.parse(String(init?.body)))
      expect(bodies.some((body) => body.symbols.includes('600000.SH'))).toBe(true)
    })
  })

  it('serves a fresh prefetched daily K-line without another request', async () => {
    const now = new Date().toISOString()
    const mockFetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/market/quotes')) return jsonResponse([])
      if (url.endsWith('/market/prefetch')) return jsonResponse({
        quotes: [], errors: {}, klines: { '600519.SH': { day: {
          symbol: '600519.SH', period: 'day', adjustment: 'hfq',
          bars: [{ timestamp: now, open: 1, high: 2, low: .5, close: 1.8, volume: 100 }],
          status: { source: 'fixture', collected_at: now, cached_at: now, delayed: false },
        } } },
      })
      if (url.includes('/market/klines/')) return jsonResponse({ detail: 'should not fetch' }, 500)
      throw new Error(`unexpected request ${url}`)
    })
    vi.stubGlobal('fetch', mockFetch)
    render(<MarketProvider><MarketProbe /></MarketProvider>)
    expect(await screen.findByText('1', { selector: '[data-testid="cached-bars"]' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '加载日线' }))
    expect(await screen.findByText('1:ok')).toBeInTheDocument()
    expect(mockFetch.mock.calls.some(([url]) => String(url).includes('/market/klines/'))).toBe(false)
  })

  it('keeps stale prefetched bars when background revalidation fails', async () => {
    const old = '2020-01-01T00:00:00Z'
    const mockFetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/market/quotes')) return jsonResponse([])
      if (url.endsWith('/market/prefetch')) return jsonResponse({
        quotes: [], errors: {}, klines: { '600519.SH': { day: {
          symbol: '600519.SH', period: 'day', adjustment: 'hfq',
          bars: [{ timestamp: old, open: 1, high: 2, low: .5, close: 1.8, volume: 100 }],
          status: { source: 'fixture', collected_at: old, cached_at: old, delayed: true, stale: true },
        } } },
      })
      if (url.includes('/market/klines/')) return jsonResponse({ detail: 'upstream unavailable' }, 503)
      throw new Error(`unexpected request ${url}`)
    })
    vi.stubGlobal('fetch', mockFetch)
    render(<MarketProvider><MarketProbe /></MarketProvider>)
    expect(await screen.findByText('1', { selector: '[data-testid="cached-bars"]' })).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '加载日线' }))
    expect(await screen.findByText('1:upstream unavailable')).toBeInTheDocument()
  })
})
