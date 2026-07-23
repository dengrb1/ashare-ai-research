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

describe('exit advice failures', () => {
  it('shows failure status before an empty action and creates a fresh retry request', async () => {
    const failed = {
      advice_id: 'failed-advice', symbol: '600519.SH', status: 'FAILED', action: null,
      decision_at: '2026-07-20T10:00:00+08:00', available_at: '2026-07-20T10:00:00+08:00',
      current_price: 1400, unrealized_profit: 1000, trigger_amount: 500, trigger_type: 'MANUAL',
      position_snapshot: { symbol: '600519.SH', name: '贵州茅台' }, research_context: {}, result: { failure_code: 'PROCESSING_FAILED' },
      cache_hit: false, created_at: '2026-07-20T10:00:00+08:00', error_message: 'PROCESSING_FAILED',
    }
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/exit-advice/manual') && init?.method === 'POST') return jsonResponse({ ...failed, advice_id: 'retry-advice', status: 'PENDING', error_message: null }, 202)
      if (url.includes('/exit-advice')) return jsonResponse([failed])
      throw new Error(`unexpected request ${url} ${init?.method}`)
    })
    vi.stubGlobal('fetch', mockFetch)
    vi.stubGlobal('crypto', { randomUUID: () => 'retry-key' })
    render(<ExitAdvicePage />)

    expect(await screen.findByText('退出研究生成失败')).toBeInTheDocument()
    expect(screen.getAllByText('生成失败').length).toBeGreaterThan(0)
    expect(screen.queryByText('等待研究')).not.toBeInTheDocument()
    expect(screen.queryByText('暂无可执行分档')).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '重新发起研究' }))
    await waitFor(() => expect(mockFetch.mock.calls.some(([url, init]) => String(url).includes('/exit-advice/manual') && init?.method === 'POST')).toBe(true))
    const call = mockFetch.mock.calls.find(([url, init]) => String(url).includes('/exit-advice/manual') && init?.method === 'POST')
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({ symbol: '600519.SH' })
    expect(new Headers(call?.[1]?.headers).get('Idempotency-Key')).toBe('retry-key')
    expect((await screen.findAllByText('研究中')).length).toBeGreaterThan(0)
  })
})
