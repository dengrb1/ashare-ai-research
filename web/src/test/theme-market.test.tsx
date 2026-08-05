import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MarketProvider, useMarket } from '../context/MarketContext'
import { MarketClosedNotice } from '../components/MarketClosedNotice'
import { ThemeProvider, ThemeToggle, useTheme } from '../context/ThemeContext'

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

function ThemeProbe() {
  const { theme, resolvedTheme } = useTheme()
  return <><span data-testid="theme">{theme}</span><span data-testid="resolved-theme">{resolvedTheme}</span><ThemeToggle /></>
}

function installColorScheme(initialDark = false) {
  let matches = initialDark
  const listeners = new Set<(event: MediaQueryListEvent) => void>()
  const media = {
    media: '(prefers-color-scheme: dark)',
    get matches() { return matches },
    onchange: null,
    addEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => listeners.add(listener),
    removeEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => listeners.delete(listener),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  } as MediaQueryList
  vi.stubGlobal('matchMedia', vi.fn(() => media))
  return (dark: boolean) => {
    matches = dark
    listeners.forEach((listener) => listener({ matches: dark, media: media.media } as MediaQueryListEvent))
  }
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

function SegmentedKlineProbe() {
  const { loadKline, getKline } = useMarket()
  const [result, setResult] = React.useState('')
  const loadSegments = async () => {
    const latest = { range: '1y' as const, start: '2026-07-10T00:00:00Z', end: '2026-07-17T00:00:00Z', chunk: 'latest', limit: 1200 }
    await loadKline('600519.SH', '1m', latest)
    await loadKline('600519.SH', '1m', latest)
    const merged = await loadKline('600519.SH', '1m', { range: '1y', start: '2026-07-03T00:00:00Z', end: '2026-07-10T00:00:00Z', chunk: 'earlier', limit: 1200 })
    setResult(merged.bars.map((bar) => bar.close).join(','))
  }
  const refreshLatest = async () => {
    const refreshed = await loadKline('600519.SH', '1m', { range: '1y', start: '2026-07-10T00:00:00Z', end: '2026-07-17T00:00:00Z', chunk: 'latest', limit: 1200, refresh: true })
    setResult(refreshed.bars.map((bar) => bar.close).join(','))
  }
  return <>
    <button onClick={() => void loadSegments()}>加载分段</button>
    <button onClick={() => void refreshLatest()}>刷新最新分段</button>
    <span data-testid="segmented-result">{result}</span>
    <span data-testid="segmented-cache">{getKline('600519.SH', '1m', { range: '1y' })?.chunks?.length || 0}</span>
  </>
}

function RevalidatingSegmentProbe() {
  const { loadKline } = useMarket()
  const [result, setResult] = React.useState('')
  const load = async () => {
    const entry = await loadKline('600519.SH', '1m', {
      range: '1d', start: '2026-07-20T01:30:00Z', end: '2026-07-20T07:30:00Z', chunk: 'latest', limit: 1200,
    })
    setResult(String(entry.bars.at(-1)?.close || ''))
  }
  return <><button onClick={() => void load()}>加载最新分段</button><span data-testid="revalidated-close">{result}</span></>
}

describe('theme system', () => {
  beforeEach(() => {
    localStorage.clear()
    document.documentElement.removeAttribute('data-theme')
    installColorScheme(false)
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('defaults to system and cycles through light, dark, and system', async () => {
    render(<ThemeProvider><ThemeProbe /></ThemeProvider>)
    expect(screen.getByTestId('theme')).toHaveTextContent('system')
    expect(screen.getByTestId('resolved-theme')).toHaveTextContent('light')
    expect(document.documentElement).toHaveAttribute('data-theme', 'light')
    await userEvent.click(screen.getByRole('button', { name: /切换浅色主题/ }))
    expect(screen.getByTestId('theme')).toHaveTextContent('light')
    expect(localStorage.getItem('ashare-theme')).toBe('light')
    await userEvent.click(screen.getByRole('button', { name: /切换深色主题/ }))
    expect(screen.getByTestId('theme')).toHaveTextContent('dark')
    expect(localStorage.getItem('ashare-theme')).toBe('dark')
    expect(document.documentElement).toHaveAttribute('data-theme', 'dark')
    await userEvent.click(screen.getByRole('button', { name: /切换跟随系统主题/ }))
    expect(screen.getByTestId('theme')).toHaveTextContent('system')
    expect(localStorage.getItem('ashare-theme')).toBe('system')
  })

  it('reacts to system theme changes immediately while in system mode', async () => {
    const setDark = installColorScheme(false)
    render(<ThemeProvider><ThemeProbe /></ThemeProvider>)
    setDark(true)
    await waitFor(() => expect(document.documentElement).toHaveAttribute('data-theme', 'dark'))
    expect(screen.getByTestId('resolved-theme')).toHaveTextContent('dark')
  })

  it('restores legacy explicit preferences and keeps them across remounts', () => {
    localStorage.setItem('ashare-theme', 'dark')
    const first = render(<ThemeProvider><ThemeProbe /></ThemeProvider>)
    expect(screen.getByTestId('theme')).toHaveTextContent('dark')
    expect(document.documentElement).toHaveAttribute('data-theme', 'dark')
    first.unmount()
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

  it('polls the server session status and only shows the close notice after final close', async () => {
    vi.useFakeTimers()
    let state = 'CLOSED'
    const mockFetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/assets')) return jsonResponse({ watchlist: [], positions: [] })
      if (url.endsWith('/market/status')) return jsonResponse({
        market_session: {
          state,
          as_of: '2026-07-20T15:00:00+08:00',
          trading_date: '2026-07-20',
          is_trading_day: true,
          reason: state === 'CLOSED' ? 'AFTER_CLOSE' : 'TRADING_SESSION',
        },
      })
      throw new Error(`unexpected request ${url}`)
    })
    vi.stubGlobal('fetch', mockFetch)
    render(<MarketProvider><MarketClosedNotice /></MarketProvider>)

    await act(async () => { await Promise.resolve() })
    expect(screen.getByRole('status')).toHaveTextContent('A 股已收盘，当前展示为收盘后行情。')

    state = 'OPEN'
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000) })
    expect(screen.queryByText('A 股已收盘，当前展示为收盘后行情。')).not.toBeInTheDocument()
    expect(mockFetch.mock.calls.filter(([url]) => String(url).endsWith('/market/status'))).toHaveLength(2)
    vi.useRealTimers()
  })

  it('prefetches watchlist plus holdings and reruns after watchlist changes', async () => {
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/assets') && (!init?.method || init.method === 'GET')) return jsonResponse({ watchlist: ['600519.SH'], positions: [{ symbol: '300750.SZ', name: '宁德时代', quantity: 100, cost: 200, target_weight: .2 }] })
      if (url.endsWith('/assets') && init?.method === 'PUT') return jsonResponse(JSON.parse(String(init.body)))
      if (url.includes('/market/quotes')) return jsonResponse([])
      if (url.endsWith('/market/prefetch')) return jsonResponse({ quotes: [], klines: {}, errors: {} })
      throw new Error(`unexpected request ${url} ${init?.method}`)
    })
    vi.stubGlobal('fetch', mockFetch)
    render(<MarketProvider><MarketProbe /></MarketProvider>)

    await waitFor(() => expect(mockFetch.mock.calls.some(([url]) => String(url).endsWith('/market/prefetch'))).toBe(true))
    const first = mockFetch.mock.calls.find(([url]) => String(url).endsWith('/market/prefetch'))
    const firstBody = JSON.parse(String(first?.[1]?.body))
    expect(firstBody.symbols).toEqual(expect.arrayContaining(['600519.SH', '300750.SZ']))
    expect(firstBody.include_quotes).toBe(false)

    await userEvent.click(screen.getByRole('button', { name: '添加自选' }))
    await waitFor(() => {
      const bodies = mockFetch.mock.calls
        .filter(([url]) => String(url).endsWith('/market/prefetch'))
        .map(([, init]) => JSON.parse(String(init?.body)))
      expect(bodies.some((body) => body.symbols.includes('600000.SH'))).toBe(true)
    })
  })

  it('forces subscribed quote refresh when the page regains focus', async () => {
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/assets') && (!init?.method || init.method === 'GET')) {
        return jsonResponse({ watchlist: ['600519.SH'], positions: [] })
      }
      if (url.includes('/market/quotes/600519.SH')) {
        return jsonResponse({
          symbol: '600519.SH', name: '贵州茅台', price: 1000, change_pct: 1,
          status: { source: 'fixture', collected_at: '2026-07-20T00:00:00Z', cached_at: '2026-07-20T00:00:00Z', delayed: false, stale: false },
        })
      }
      if (url.includes('/market/quotes')) return jsonResponse([])
      if (url.endsWith('/market/prefetch')) return jsonResponse({ quotes: [], klines: {}, errors: {} })
      throw new Error(`unexpected request ${url}`)
    })
    vi.stubGlobal('fetch', mockFetch)
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
    render(<MarketProvider><MarketProbe /></MarketProvider>)

    await waitFor(() => expect(mockFetch.mock.calls.some(([url]) => String(url).includes('/market/quotes/600519.SH'))).toBe(true))
    window.dispatchEvent(new Event('focus'))

    await waitFor(() => expect(mockFetch.mock.calls.some(([url]) => String(url).includes('/market/quotes/600519.SH?refresh=true'))).toBe(true))
  })

  it('loads the focused quote before deferring K-line prefetch and background quotes', async () => {
    let resolveQuotes: (response: Response) => void
    const quotes = new Promise<Response>((resolve) => { resolveQuotes = resolve })
    const mockFetch = vi.fn((input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/assets')) return Promise.resolve(jsonResponse({ watchlist: ['600519.SH'], positions: [{ symbol: '000001.SZ', name: '平安银行', quantity: 100, cost: 10 }] }))
      if (url.includes('/market/quotes/')) return quotes
      if (url.includes('/market/quotes')) return Promise.resolve(jsonResponse([]))
      if (url.endsWith('/market/prefetch')) return Promise.resolve(jsonResponse({ quotes: [], klines: {}, errors: {} }))
      throw new Error(`unexpected request ${url}`)
    })
    vi.stubGlobal('fetch', mockFetch)
    render(<MarketProvider><MarketProbe /></MarketProvider>)

    await waitFor(() => expect(mockFetch.mock.calls.filter(([url]) => String(url).includes('/market/quotes')).length).toBe(1))
    expect(mockFetch.mock.calls.some(([url]) => String(url).endsWith('/market/prefetch'))).toBe(false)

    resolveQuotes!(jsonResponse({
      symbol: '600519.SH', name: '贵州茅台', price: 1000, change_pct: 1,
      status: { source: 'fixture', collected_at: '2026-07-20T00:00:00Z', cached_at: '2026-07-20T00:00:00Z', delayed: false, stale: false },
    }))
    await waitFor(() => expect(mockFetch.mock.calls.some(([url]) => String(url).endsWith('/market/prefetch'))).toBe(true))

    const prefetch = mockFetch.mock.calls.find(([url]) => String(url).endsWith('/market/prefetch'))
    expect(JSON.parse(String(prefetch?.[1]?.body))).toMatchObject({
      symbols: ['000001.SZ', '600519.SH'], include_quotes: false,
    })
    expect(mockFetch.mock.calls.filter(([url]) => String(url).includes('/market/quotes'))).toHaveLength(2)
  })

  it('serves a fresh prefetched daily K-line without another request', async () => {
    const now = new Date().toISOString()
    const mockFetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/assets')) return jsonResponse({ watchlist: ['600519.SH'], positions: [] })
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
      if (url.endsWith('/assets')) return jsonResponse({ watchlist: ['600519.SH'], positions: [] })
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

  it('isolates and merges range chunks, deduplicates timestamps, and refreshes only the latest chunk', async () => {
    let latestCalls = 0
    const mockFetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/assets')) return jsonResponse({ watchlist: [], positions: [] })
      if (url.includes('/market/klines/')) {
        const parsed = new URL(url, 'http://localhost')
        const start = parsed.searchParams.get('start')
        if (start === '2026-07-10T00:00:00Z') {
          latestCalls += 1
          return jsonResponse({
            symbol: '600519.SH', period: '1m', adjustment: 'hfq',
            bars: [
              { timestamp: '2026-07-10T01:31:00Z', open: 10, high: 10, low: 10, close: 10, volume: 1 },
              { timestamp: '2026-07-11T01:31:00Z', open: 11, high: latestCalls > 1 ? 99 : 11, low: 11, close: latestCalls > 1 ? 99 : 11, volume: 1 },
            ],
            status: { source: 'fixture', collected_at: '2026-07-17T00:00:00Z', cached_at: '2026-07-17T00:00:00Z', delayed: false },
          })
        }
        return jsonResponse({
          symbol: '600519.SH', period: '1m', adjustment: 'hfq',
          bars: [
            { timestamp: '2026-07-05T01:31:00Z', open: 5, high: 5, low: 5, close: 5, volume: 1 },
            { timestamp: '2026-07-10T01:31:00Z', open: 9, high: 9, low: 9, close: 9, volume: 1 },
          ],
          status: { source: 'fixture', collected_at: '2026-07-17T00:00:00Z', cached_at: '2026-07-17T00:00:00Z', delayed: false },
        })
      }
      throw new Error(`unexpected request ${url}`)
    })
    vi.stubGlobal('fetch', mockFetch)
    render(<MarketProvider><SegmentedKlineProbe /></MarketProvider>)

    await userEvent.click(screen.getByRole('button', { name: '加载分段' }))
    expect(await screen.findByText('5,9,11')).toBeInTheDocument()
    expect(screen.getByTestId('segmented-cache')).toHaveTextContent('2')
    expect(mockFetch.mock.calls.filter(([url]) => String(url).includes('/market/klines/'))).toHaveLength(2)

    await userEvent.click(screen.getByRole('button', { name: '刷新最新分段' }))
    expect(await screen.findByText('5,10,99')).toBeInTheDocument()
    const klineUrls = mockFetch.mock.calls.filter(([url]) => String(url).includes('/market/klines/')).map(([url]) => String(url))
    expect(klineUrls).toHaveLength(3)
    expect(klineUrls[2]).toContain('refresh=true')
  })

  it('revalidates an expired latest range chunk instead of serving it forever', async () => {
    const baseNow = Date.now()
    const clock = vi.spyOn(Date, 'now').mockReturnValue(baseNow)
    let klineCalls = 0
    const mockFetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/assets')) return jsonResponse({ watchlist: [], positions: [] })
      if (url.includes('/market/klines/')) {
        klineCalls += 1
        return jsonResponse({
          symbol: '600519.SH', period: '1m', adjustment: 'hfq',
          bars: [{ timestamp: '2026-07-20T07:00:00Z', open: 10, high: 10, low: klineCalls, close: klineCalls, volume: 1 }],
          status: { source: 'fixture', collected_at: '2026-07-20T07:30:00Z', cached_at: '2026-07-20T07:30:00Z', delayed: false },
        })
      }
      throw new Error(`unexpected request ${url}`)
    })
    vi.stubGlobal('fetch', mockFetch)
    render(<MarketProvider><RevalidatingSegmentProbe /></MarketProvider>)

    await userEvent.click(screen.getByRole('button', { name: '加载最新分段' }))
    expect(await screen.findByTestId('revalidated-close')).toHaveTextContent('1')
    await userEvent.click(screen.getByRole('button', { name: '加载最新分段' }))
    expect(klineCalls).toBe(1)

    clock.mockReturnValue(baseNow + 31_000)
    await userEvent.click(screen.getByRole('button', { name: '加载最新分段' }))
    expect(await screen.findByTestId('revalidated-close')).toHaveTextContent('2')
    expect(klineCalls).toBe(2)
    clock.mockRestore()
  })
})
