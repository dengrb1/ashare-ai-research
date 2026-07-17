import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api'

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

describe('API security and market adapters', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
    document.cookie = 'ashare_csrf=csrf-value; path=/'
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    document.cookie = 'ashare_csrf=; Max-Age=0; path=/'
  })

  it('sends cookie credentials and the configured CSRF token for mutations', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ run_id: 'run-1', status: 'PENDING' }))
    await api.submitResearch('2026-07-15')

    const [, init] = vi.mocked(fetch).mock.calls[0]
    const headers = new Headers(init?.headers)
    expect(init?.credentials).toBe('include')
    expect(headers.get('X-CSRF-Token')).toBe('csrf-value')
    expect(init?.method).toBe('POST')
  })

  it('normalizes backend quote names and nested status metadata', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse([{
      symbol: '600519.SH', name: '贵州茅台', price: 1500, change: 15,
      change_percent: 1.01, previous_close: 1485,
      status: { source: 'AKShare', collected_at: '2026-07-15T06:30:00Z', cached_at: '2026-07-15T06:30:01Z', delayed: true },
    }]))

    const rows = await api.quotes(['600519.SH'])
    expect(rows[0]).toMatchObject({ change_pct: 1.01, prev_close: 1485, source: 'AKShare', delayed: true })
  })

  it('maps kline timestamps while retaining hfq adjustment request', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({
      symbol: '600519.SH', period: 'day', adjustment: 'hfq',
      bars: [{ timestamp: '2026-07-15T07:00:00Z', open: 1, high: 2, low: .5, close: 1.8, volume: 100 }],
      status: { source: 'AKShare', collected_at: '2026-07-15T07:00:00Z', cached_at: '2026-07-15T07:00:01Z', delayed: false },
    }))

    const payload = await api.kline('600519.SH', 'day')
    expect(payload.bars[0].time).toBe('2026-07-15T07:00:00Z')
    expect(String(vi.mocked(fetch).mock.calls[0][0])).toContain('adjust=hfq')
  })

  it('uses the NeoData financial search endpoint for natural-language queries', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({
      query: '贵州茅台股价', provider: 'neodata-financial-search', upstream: 'sina-finance', mode: 'embedded',
      searched_at: '2026-07-15T11:00:00Z', elapsed_ms: 20,
      entities: [{ name: '贵州茅台', code: 'sh600519' }],
      recalls: [{ type: 'basic_info', desc: '行情数据', content: '最新: 1500' }],
      raw_sha256: 'a'.repeat(64), live_data_isolated_from_snapshots: true,
    }))

    const result = await api.financialSearch('贵州茅台股价')
    expect(result.provider).toBe('neodata-financial-search')
    expect(result.recalls[0].content).toBe('最新: 1500')
    expect(String(vi.mocked(fetch).mock.calls[0][0])).toContain('/search/financial?q=')
  })
})
