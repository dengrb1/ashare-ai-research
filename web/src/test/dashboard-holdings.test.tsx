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
