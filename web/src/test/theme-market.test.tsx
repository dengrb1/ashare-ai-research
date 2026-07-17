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
})
