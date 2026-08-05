import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, streamAIChat } from '../api'
import { resolvePublishedResearchRun } from '../researchRuns'

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

  it('uses the focused quote endpoint and persists refresh settings through the narrow API', async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(jsonResponse({ symbol: '600519.SH', price: 21.8, status: { source: 'sina', collected_at: '2026-07-20T00:00:00Z', cached_at: '2026-07-20T00:00:00Z', delayed: false, stale: false } }))
      .mockResolvedValueOnce(jsonResponse({ watchlist: [], positions: [], market_refresh_interval_seconds: 30 }))

    const quote = await api.quote('600519.SH', true)
    await api.saveMarketRefreshSettings(30)

    expect(quote.price).toBe(21.8)
    expect(String(vi.mocked(fetch).mock.calls[0][0])).toContain('/market/quotes/600519.SH?refresh=true')
    expect(String(vi.mocked(fetch).mock.calls[1][0])).toContain('/assets/market-refresh')
    const init = vi.mocked(fetch).mock.calls[1][1]
    expect(init?.method).toBe('PUT')
    expect(new Headers(init?.headers).get('Idempotency-Key')).toBeTruthy()
    expect(JSON.parse(String(init?.body))).toEqual({ market_refresh_interval_seconds: 30 })
  })

  it('targets single-score lineage and research cancellation endpoints', async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(jsonResponse({ symbol: '600000.SH', total_score: 80 }))
      .mockResolvedValueOnce(jsonResponse({ symbol: '600000.SH', formula_version: 'v1' }))
      .mockResolvedValueOnce(jsonResponse({ run_id: 'run-1', status: 'CANCEL_REQUESTED' }))

    await api.score('2026-07-17', '600000.SH', 'run-1')
    await api.scoreLineage('2026-07-17', '600000.SH', 'run-1')
    await api.cancelResearch('run-1')

    expect(String(vi.mocked(fetch).mock.calls[0][0])).toContain('/scores/2026-07-17/600000.SH?run_id=run-1')
    expect(String(vi.mocked(fetch).mock.calls[1][0])).toContain('/scores/2026-07-17/600000.SH/lineage?run_id=run-1')
    expect(String(vi.mocked(fetch).mock.calls[2][0])).toContain('/research/runs/run-1/cancel')
    expect(vi.mocked(fetch).mock.calls[2][1]?.method).toBe('POST')
  })

  it('lets an explicit published run override a mismatched date', async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(jsonResponse([]))
      .mockResolvedValueOnce(jsonResponse({
        run_id: 'fused-run', run_type: 'DAILY', trading_date: '2026-07-16', status: 'FUSED',
      }))
      .mockResolvedValueOnce(jsonResponse([{
        run_id: 'fused-run', trading_date: '2026-07-16', status: 'FUSED', reason_code: 'DATA_GATE',
      }]))

    const selected = await resolvePublishedResearchRun('2026-07-17', 'fused-run')

    expect(selected).toMatchObject({ run_id: 'fused-run', trading_date: '2026-07-16', status: 'FUSED' })
    expect(String(vi.mocked(fetch).mock.calls[0][0])).toContain('published=true')
    expect(String(vi.mocked(fetch).mock.calls[1][0])).toContain('/runs/fused-run')
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

  it('maps kline timestamps while requesting the live-compatible raw basis', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({
      symbol: '600519.SH', period: 'day', adjustment: 'hfq',
      bars: [{ timestamp: '2026-07-15T07:00:00Z', open: 1, high: 2, low: .5, close: 1.8, volume: 100 }],
      status: { source: 'AKShare', collected_at: '2026-07-15T07:00:00Z', cached_at: '2026-07-15T07:00:01Z', delayed: false },
    }))

    const payload = await api.kline('600519.SH', 'day', 1200, { start: '2026-06-01T00:00:00+08:00', end: '2026-07-01T00:00:00+08:00' })
    expect(payload.bars[0].time).toBe('2026-07-15T07:00:00Z')
    const url = String(vi.mocked(fetch).mock.calls[0][0])
    expect(url).toContain('adjust=raw')
    expect(url).toContain('limit=1200')
    expect(url).toContain('start=2026-06-01T00%3A00%3A00%2B08%3A00')
    expect(url).toContain('end=2026-07-01T00%3A00%3A00%2B08%3A00')
  })

  it('bypasses client/server market caches and sends CSRF for backtest retry', async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(jsonResponse([]))
      .mockResolvedValueOnce(jsonResponse({ symbol: '600519.SH', period: 'day', adjustment: 'hfq', bars: [], status: { source: 'fixture', collected_at: '2026-07-17T00:00:00Z', cached_at: '2026-07-17T00:00:00Z', delayed: false } }))
      .mockResolvedValueOnce(jsonResponse({ backtest_id: 'backtest-1', run_id: 'run-1', status: 'PENDING', retry_count: 1 }))
    await api.quotes(['600519.SH'], true)
    await api.kline('600519.SH', 'day', 160, true)
    await api.retryBacktest('backtest-1')

    expect(String(vi.mocked(fetch).mock.calls[0][0])).toContain('refresh=true')
    expect(String(vi.mocked(fetch).mock.calls[1][0])).toContain('refresh=true')
    const retry = vi.mocked(fetch).mock.calls[2]
    expect(retry[1]?.method).toBe('POST')
    expect(new Headers(retry[1]?.headers).get('X-CSRF-Token')).toBe('csrf-value')
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

  it('sends model settings mutations with CSRF without expecting a returned API key', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({
      configuration_id: 'config-1', version: 1, config_sha256: 'a'.repeat(64), source: 'database',
      provider: 'openai-compatible', base_url: 'https://gateway.example/v1', api_key_configured: true,
      search_model: 'gpt-5.6-luna', search_reasoning_effort: 'low', research_model: 'gpt-5.6-sol',
      research_reasoning_effort: 'high', model_profiles: [], timeout_seconds: 90, enabled: true, configured: true,
      reachable: true, degraded: false, status_message: 'ok',
    }))
    await api.saveModelSettings({
      base_url: 'https://gateway.example/v1', api_key: '', search_model: 'gpt-5.6-luna',
      search_reasoning_effort: 'low', research_model: 'gpt-5.6-sol', research_reasoning_effort: 'high',
      model_profiles: [], timeout_seconds: 90, enabled: true,
    })

    const [, init] = vi.mocked(fetch).mock.calls[0]
    expect(init?.method).toBe('PUT')
    expect(new Headers(init?.headers).get('X-CSRF-Token')).toBe('csrf-value')
    expect(String(init?.body)).toContain('"api_key":""')
  })

  it('reads system resources and protects every settings mutation with an unlock token', async () => {
    const resources = {
      collected_at: '2026-07-26T08:00:00Z', scope: 'CONTAINER', scope_label: 'Docker',
      memory: { total_bytes: 100, used_bytes: 40, available_bytes: 60, percent: 40 },
      cpu: { percent: 12, logical_cores: 8 },
      disk: { total_bytes: 200, used_bytes: 50, available_bytes: 150, percent: 25 },
      services: [],
      topology_estimate: { worker_replicas: 1, typical_per_worker_bytes: 20, typical_increment_bytes: 20, maximum_increment_bytes: 40, projected_available_bytes: 40, level: 'NORMAL', messages: [] },
    }
    const settings = { values: {}, workers: [], queues: {} }
    vi.mocked(fetch)
      .mockResolvedValueOnce(jsonResponse(resources))
      .mockResolvedValueOnce(jsonResponse({ unlock_token: 'unlock-secret', expires_at: '2026-07-26T08:10:00Z' }))
      .mockResolvedValueOnce(jsonResponse(settings))
      .mockResolvedValueOnce(jsonResponse(settings))
      .mockResolvedValueOnce(jsonResponse(settings))

    expect(await api.systemResources()).toMatchObject({ scope: 'CONTAINER', cpu: { logical_cores: 8 } })
    expect(await api.unlockSystemSettings('current-password')).toMatchObject({ unlock_token: 'unlock-secret' })
    await api.saveSystemSettings({ market_cache_seconds: 30 }, 'unlock-secret', 'save-key')
    await api.restoreSystemSetting('market_cache_seconds', 'unlock-secret')
    await api.restoreAllSystemSettings('unlock-secret')

    expect(String(vi.mocked(fetch).mock.calls[0][0])).toContain('/admin/system-resources')
    expect(JSON.parse(String(vi.mocked(fetch).mock.calls[1][1]?.body))).toEqual({ password: 'current-password' })
    expect(vi.mocked(fetch).mock.calls[1][1]?.method).toBe('POST')
    for (const call of vi.mocked(fetch).mock.calls.slice(2)) {
      expect(new Headers(call[1]?.headers).get('X-System-Settings-Unlock')).toBe('unlock-secret')
    }
    expect(new Headers(vi.mocked(fetch).mock.calls[2][1]?.headers).get('Idempotency-Key')).toBe('save-key')
  })

  it('persists user asset state with CSRF protection', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ watchlist: ['600000.SH'], positions: [] }))
    await api.saveAssets({ watchlist: ['600000.SH'], positions: [] })

    const [, init] = vi.mocked(fetch).mock.calls[0]
    expect(init?.method).toBe('PUT')
    expect(new Headers(init?.headers).get('X-CSRF-Token')).toBe('csrf-value')
    expect(String(init?.body)).toContain('600000.SH')
  })

  it('uploads chat images without JSON encoding and applies imports idempotently', async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(jsonResponse([]))
      .mockResolvedValueOnce(jsonResponse({ archive_id: 'apply-1', kind: 'IMPORT_APPLY', status: 'PENDING', phase: 'QUEUED', progress: 0, created_at: '2026-07-20T00:00:00Z', expires_at: '2026-07-21T00:00:00Z' }, 202))
    const image = new File(['png'], 'chart.png', { type: 'image/png' })

    await api.uploadAIChatAttachments([image], 'thread-1')
    await api.applyPersonalImport('preview-1', { total_assets: 'CURRENT' }, 'stable-apply-key')

    const upload = vi.mocked(fetch).mock.calls[0]
    expect(upload[1]?.body).toBeInstanceOf(FormData)
    expect(new Headers(upload[1]?.headers).get('Content-Type')).toBeNull()
    expect(new Headers(upload[1]?.headers).get('X-CSRF-Token')).toBe('csrf-value')
    const apply = vi.mocked(fetch).mock.calls[1]
    expect(new Headers(apply[1]?.headers).get('Idempotency-Key')).toBe('stable-apply-key')
    expect(String(apply[1]?.body)).toContain('total_assets')
  })

  it('retries chat before the first delta with the same idempotency key', async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(new Response('', { status: 503 }))
      .mockResolvedValueOnce(new Response(
        'data: {"type":"delta","delta":"ok"}\n\ndata: {"type":"done","status":"COMPLETED"}\n\n',
        { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
      ))
    const events: Array<Record<string, unknown>> = []

    await streamAIChat(
      'thread-1',
      { content: 'hello', model: 'gpt-test', reasoning_effort: 'medium', web_search: false },
      (event) => events.push(event),
      undefined,
      'stable-chat-key',
    )

    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(2)
    expect(vi.mocked(fetch).mock.calls.map((call) => new Headers(call[1]?.headers).get('Idempotency-Key'))).toEqual([
      'stable-chat-key', 'stable-chat-key',
    ])
    expect(events.at(-1)?.type).toBe('done')
  })

  it('rejects a chat stream that ends after partial text without done', async () => {
    vi.mocked(fetch).mockResolvedValue(new Response(
      'data: {"type":"delta","delta":"partial"}\n\n',
      { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
    ))

    await expect(streamAIChat(
      'thread-1',
      { content: 'hello', model: 'gpt-test', reasoning_effort: 'medium', web_search: false },
      () => undefined,
      undefined,
      'partial-chat-key',
    )).rejects.toMatchObject({ code: 'CHAT_STREAM_INCOMPLETE' })
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(1)
  })
})
