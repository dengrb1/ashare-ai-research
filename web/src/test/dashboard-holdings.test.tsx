import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { MarketProvider } from '../context/MarketContext'
import { DashboardPage } from '../pages/DashboardPage'

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

it('keeps the holdings panel stable while assets are loading', async () => {
  let resolveAssets: (response: Response) => void
  const assets = new Promise<Response>((resolve) => { resolveAssets = resolve })
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.endsWith('/assets')) return assets
    if (url.endsWith('/runs')) return Promise.resolve(jsonResponse([]))
    if (url.endsWith('/notifications/summary')) return Promise.resolve(jsonResponse({ unread_count: 0, high_risk_unread_count: 0, latest: [] }))
    if (url.includes('/market/quotes')) return Promise.resolve(jsonResponse([]))
    if (url.endsWith('/market/prefetch')) return Promise.resolve(jsonResponse({ quotes: [], klines: {}, errors: {} }))
    throw new Error(`unexpected request ${url}`)
  }))

  render(<MarketProvider><DashboardPage /></MarketProvider>)
  expect(screen.getByRole('status', { name: '持仓数据加载中' })).toBeInTheDocument()
  expect(screen.queryByText('暂无模拟持仓')).not.toBeInTheDocument()

  resolveAssets!(jsonResponse({ watchlist: [], positions: [] }))
  await waitFor(() => expect(screen.queryByRole('status', { name: '持仓数据加载中' })).not.toBeInTheDocument())
  expect(screen.getByText('暂无模拟持仓')).toBeInTheDocument()
})

it('shows total and per-position PnL with an explicit cost-price fallback', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.endsWith('/assets')) return jsonResponse({
      watchlist: [],
      total_assets: 300000,
      positions: [
        { symbol: '600000.SH', name: '浦发银行', quantity: 1000, cost: 10 },
        { symbol: '000001.SZ', name: '平安银行', quantity: 1000, cost: 12 },
      ],
    })
    if (url.includes('/market/quotes')) return jsonResponse([
      { symbol: '600000.SH', name: '浦发银行', price: 11, change_pct: 1 },
    ])
    if (url.endsWith('/notifications/summary')) return jsonResponse({ unread_count: 0, high_risk_unread_count: 0, latest: [] })
    if (url.includes('/market/klines/')) return jsonResponse({ symbol: '600000.SH', period: 'day', bars: [{ time: '2026-07-17', open: 10, high: 11, low: 9, close: 11, volume: 1000 }], status: {} })
    if (url.endsWith('/market/prefetch')) return jsonResponse({ quotes: [], klines: {}, errors: {} })
    if (url.endsWith('/runs')) return jsonResponse([])
    throw new Error(`unexpected request ${url}`)
  }))

  render(<MarketProvider><DashboardPage /></MarketProvider>)
  expect(await screen.findByText('模拟持仓盈亏')).toBeInTheDocument()
  await waitFor(() => expect(screen.getAllByText('+1,000')).toHaveLength(2))
  expect(screen.getAllByText('成本价估算')).toHaveLength(1)
  expect(screen.getByText('+4.55%')).toBeInTheDocument()
})

it('renders the watchlist daily trend and recent dashboard activity', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.endsWith('/assets')) return jsonResponse({ watchlist: ['600519.SH'], positions: [] })
    if (url.endsWith('/market/quotes/600519.SH')) return jsonResponse({ symbol: '600519.SH', name: '贵州茅台', price: 1500, change_pct: 1.2, amount: 1200000 })
    if (url.includes('/market/quotes')) return jsonResponse([{ symbol: '600519.SH', name: '贵州茅台', price: 1500, change_pct: 1.2, amount: 1200000 }])
    if (url.includes('/market/klines/')) return jsonResponse({ symbol: '600519.SH', period: 'day', bars: [
      { time: '2026-07-16', open: 1490, high: 1510, low: 1480, close: 1495, volume: 1000 },
      { time: '2026-07-17', open: 1495, high: 1520, low: 1490, close: 1500, volume: 1100 },
    ], status: {} })
    if (url.endsWith('/market/prefetch')) return jsonResponse({ quotes: [], klines: {}, errors: {} })
    if (url.endsWith('/notifications/summary')) return jsonResponse({ unread_count: 1, high_risk_unread_count: 0, latest: [{
      notification_id: 'notice-1', notification_type: '研究提醒', severity: 'INFO', title: '贵州茅台研究完成', body: '最新研究结果已发布。', payload: {}, created_at: '2026-07-17T08:00:00+08:00', expires_at: '2026-07-18T08:00:00+08:00',
    }] })
    if (url.endsWith('/runs')) return jsonResponse([])
    throw new Error(`unexpected request ${url}`)
  }))

  render(<MarketProvider><DashboardPage /></MarketProvider>)
  expect(await screen.findByLabelText('贵州茅台 日线走势')).toBeInTheDocument()
  expect(screen.getByText('贵州茅台研究完成')).toBeInTheDocument()
  expect(screen.getByText('研究提醒')).toBeInTheDocument()
})
