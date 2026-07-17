import type { AssetState, AuditEvent, Candidate, DataEnvelope, FinancialSearchResult, FinancialSearchStatus, KlineBar, MarketPrefetchResponse, ModelSettings, ModelSettingsDraft, Portfolio, Quote, Report, Run, Score, Snapshot, User } from './types'

const API_BASE = (import.meta.env.VITE_API_BASE || '/api/v1').replace(/\/$/, '')

export class ApiError extends Error {
  status: number
  detail: string

  constructor(status: number, detail: string) {
    super(detail)
    this.status = status
    this.detail = detail
  }
}

function readCookie(name: string) {
  const prefix = `${name}=`
  return document.cookie.split(';').map((item) => item.trim()).find((item) => item.startsWith(prefix))?.slice(prefix.length)
}

function csrfToken() {
  const configured = import.meta.env.VITE_CSRF_COOKIE_NAME || 'ashare_csrf'
  return decodeURIComponent(readCookie(configured) || readCookie('ashare_csrf') || readCookie('csrf_token') || readCookie('csrftoken') || readCookie('XSRF-TOKEN') || '')
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method || 'GET').toUpperCase()
  const headers = new Headers(init.headers)
  if (init.body && !(init.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
    const token = csrfToken()
    if (token) {
      headers.set('X-CSRF-Token', token)
      headers.set('X-CSRFToken', token)
    }
  }
  const response = await fetch(`${API_BASE}${path}`, { ...init, headers, credentials: 'include' })
  if (response.status === 204) return undefined as T
  const type = response.headers.get('content-type') || ''
  const payload = type.includes('application/json') ? await response.json() : await response.text()
  if (!response.ok) {
    const detail = typeof payload === 'object' && payload ? payload.detail || payload.message : payload
    throw new ApiError(response.status, detail || `请求失败 (${response.status})`)
  }
  return payload as T
}

function params(values: Record<string, string | number | undefined>) {
  const query = new URLSearchParams()
  Object.entries(values).forEach(([key, value]) => value !== undefined && query.set(key, String(value)))
  const encoded = query.toString()
  return encoded ? `?${encoded}` : ''
}

export const api = {
  login: (username: string, password: string) => request<User>('/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) }),
  logout: () => request<void>('/auth/logout', { method: 'POST' }),
  me: () => request<User>('/auth/me'),
  assets: () => request<AssetState>('/assets'),
  saveAssets: (payload: AssetState) => request<AssetState>('/assets', { method: 'PUT', body: JSON.stringify(payload) }),

  quotes: async (symbols: string[]) => {
    const rows = await request<Array<Quote & { change_percent?: number; previous_close?: number }>>(`/market/quotes${params({ symbols: symbols.join(',') })}`)
    return rows.map((row) => ({
      ...row,
      price: row.price ?? 0,
      change_pct: row.change_pct ?? row.change_percent ?? 0,
      prev_close: row.prev_close ?? row.previous_close,
      source: row.source || row.status?.source,
      collected_at: row.collected_at || row.status?.collected_at,
      cached_at: row.cached_at || row.status?.cached_at,
      delayed: row.delayed ?? row.status?.delayed,
      is_delayed: row.is_delayed ?? row.status?.delayed,
    })) as Quote[]
  },
  kline: async (symbol: string, period: string, limit = 160) => {
    const payload = await request<DataEnvelope<KlineBar[]> & { bars: KlineBar[] }>(`/market/klines/${encodeURIComponent(symbol)}${params({ period, limit, adjust: 'hfq' })}`)
    return { ...payload, bars: (payload.bars || []).map((bar) => ({ ...bar, time: bar.time || bar.timestamp })) }
  },
  prefetchMarket: async (symbols: string[], periods = ['day'], limit = 160) => {
    const payload = await request<MarketPrefetchResponse>('/market/prefetch', {
      method: 'POST',
      body: JSON.stringify({ symbols, periods, limit }),
    })
    if (!payload || Array.isArray(payload)) return { quotes: [], klines: {}, errors: {} } as MarketPrefetchResponse
    return {
      ...payload,
      quotes: (payload.quotes || []).map((row) => ({
        ...row,
        price: row.price ?? 0,
        change_pct: row.change_pct ?? (row as Quote & { change_percent?: number }).change_percent ?? 0,
        prev_close: row.prev_close ?? (row as Quote & { previous_close?: number }).previous_close,
        source: row.source || row.status?.source,
        collected_at: row.collected_at || row.status?.collected_at,
        cached_at: row.cached_at || row.status?.cached_at,
        delayed: row.delayed ?? row.status?.delayed,
      })),
      klines: payload.klines || {},
      errors: payload.errors || {},
    }
  },
  marketStatus: () => request<Record<string, unknown>>('/market/status'),
  financialSearch: (query: string) => request<FinancialSearchResult>(`/search/financial${params({ q: query })}`),
  financialSearchStatus: () => request<FinancialSearchStatus>('/search/status'),

  scores: (date: string, runId?: string) => request<Score[]>(`/scores/${date}${params({ run_id: runId })}`),
  candidates: (date: string, runId?: string) => request<Candidate[]>(`/candidates/${date}${params({ run_id: runId })}`),
  portfolio: (date: string, runId?: string) => request<Portfolio>(`/portfolios/${date}${params({ run_id: runId })}`),
  report: (date: string, runId?: string) => request<Report>(`/reports/${date}${params({ run_id: runId })}`),
  reportContent: (reportId: string) => request<{ content?: string; body?: string }>(`/reports/${reportId}/content`),

  submitResearch: (tradingDate: string) => request<Run>('/research/runs', { method: 'POST', body: JSON.stringify({ trading_date: tradingDate }) }),
  runs: () => request<Run[] | { items: Run[] }>('/runs'),
  run: (runId: string) => request<Run>(`/runs/${runId}`),
  audit: (runId: string) => request<AuditEvent[]>(`/runs/${runId}/audit`),
  submitBacktest: (payload: { name: string; start_date: string; end_date: string; snapshot_ids: string[]; config: Record<string, unknown> }) => request<Run>('/backtests', { method: 'POST', body: JSON.stringify(payload) }),
  backtest: (backtestId: string) => request<Run>(`/backtests/${backtestId}`),
  backtests: () => request<Run[] | { items: Run[] }>('/backtests'),
  snapshots: () => request<Snapshot[]>('/snapshots?dataset=backtest_bundle'),

  users: () => request<User[]>('/admin/users'),
  createUser: (payload: { username: string; password: string; role: string }) => request<User>('/admin/users', { method: 'POST', body: JSON.stringify({ ...payload, role: payload.role.toUpperCase() }) }),
  setUserDisabled: (id: string, disabled: boolean) => request<User>(`/admin/users/${id}`, { method: 'PATCH', body: JSON.stringify({ enabled: !disabled }) }),
  resetPassword: (id: string, password: string) => request<User>(`/admin/users/${id}`, { method: 'PATCH', body: JSON.stringify({ password }) }),
  modelSettings: () => request<ModelSettings>('/admin/model-settings'),
  testModelSettings: (payload: ModelSettingsDraft) => request<{ reachable: boolean; message: string; model: string; checked_at: string }>('/admin/model-settings/test', { method: 'POST', body: JSON.stringify(payload) }),
  saveModelSettings: (payload: ModelSettingsDraft) => request<ModelSettings>('/admin/model-settings', { method: 'PUT', body: JSON.stringify(payload) }),
  listModels: (payload: ModelSettingsDraft) => request<{ models: string[] }>('/admin/model-settings/models', { method: 'POST', body: JSON.stringify(payload) }),
}

export function unwrapList<T>(payload: T[] | { items?: T[]; data?: T[] }) {
  if (Array.isArray(payload)) return payload
  return payload.items || payload.data || []
}

export function unwrapEnvelope<T>(payload: DataEnvelope<T> | T, keys: Array<keyof DataEnvelope<T>>): { data: T; meta: Omit<DataEnvelope<T>, 'data' | 'items' | 'quotes' | 'bars'> } {
  if (Array.isArray(payload)) return { data: payload as T, meta: {} }
  const object = payload as DataEnvelope<T>
  let data: T | undefined
  for (const key of keys) {
    const value = object[key]
    if (value !== undefined) {
      data = value as T
      break
    }
  }
  return { data: data ?? (payload as T), meta: object }
}
