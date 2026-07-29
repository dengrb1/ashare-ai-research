import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ExitAdvicePage } from '../pages/ExitAdvicePage'

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('trade advice page', () => {
  it('loads watchlist advice and saves manual prices with an idempotency key', async () => {
    const initialMonitor = {
      monitor_id: 'monitor-1', symbol: '600519.SH', enabled: true,
      manual_buy_price: null, manual_sell_price: null, ai_buy_price: 1450, ai_sell_price: 1550,
      stop_loss_price: 1350, rationale: { summary: '基于最新研究生成的模拟交易建议。' },
      generated_for: '2026-07-29', generated_at: '2026-07-29T09:30:00+08:00',
      model_name: 'test-model', model_source: 'fixture', last_alert_at: null, last_alert_types: [],
      error_code: null, created_at: '2026-07-29T09:30:00+08:00', updated_at: '2026-07-29T09:30:00+08:00',
    }
    const savedBodies: unknown[] = []
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/assets')) return jsonResponse({ watchlist: ['600519.SH'], positions: [] })
      if (url.endsWith('/trade-advice-monitors') && init?.method === 'PUT') {
        const body = JSON.parse(String(init.body))
        savedBodies.push(body)
        expect(new Headers(init.headers).get('Idempotency-Key')).toBe('manual-price-key')
        return jsonResponse({ ...initialMonitor, ...body, updated_at: '2026-07-29T09:31:00+08:00' })
      }
      if (url.endsWith('/trade-advice-monitors')) return jsonResponse([initialMonitor])
      throw new Error(`unexpected request ${url} ${init?.method}`)
    })
    vi.stubGlobal('fetch', mockFetch)
    vi.stubGlobal('crypto', { randomUUID: () => 'manual-price-key' })

    render(<ExitAdvicePage />)

    expect(await screen.findByText('600519.SH')).toBeInTheDocument()
    expect(screen.getByText('AI 买入目标')).toBeInTheDocument()
    expect(screen.getByText('基于最新研究生成的模拟交易建议。')).toBeInTheDocument()

    await userEvent.clear(screen.getByLabelText('自定义买入价'))
    await userEvent.type(screen.getByLabelText('自定义买入价'), '1400')
    await userEvent.clear(screen.getByLabelText('自定义卖出价'))
    await userEvent.type(screen.getByLabelText('自定义卖出价'), '1600')
    await userEvent.click(screen.getByRole('button', { name: '保存自定义价格' }))

    await waitFor(() => expect(savedBodies).toContainEqual({
      symbol: '600519.SH', enabled: true, manual_buy_price: 1400, manual_sell_price: 1600,
    }))
  })

  it('shows the empty state when no watchlist symbols are available', async () => {
    const mockFetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/assets')) return jsonResponse({ watchlist: [], positions: [] })
      if (url.endsWith('/trade-advice-monitors')) return jsonResponse([])
      throw new Error(`unexpected request ${url}`)
    })
    vi.stubGlobal('fetch', mockFetch)

    render(<ExitAdvicePage />)

    expect(await screen.findByText('暂无自选股')).toBeInTheDocument()
    expect(screen.getByText('请先在行情或资产页面添加自选股。')).toBeInTheDocument()
  })
})
