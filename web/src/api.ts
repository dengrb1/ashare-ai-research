import type { AIChatAttachment, AIChatMessage, AIChatThread, AICostSummary, AssetState, AuditEvent, BuyEntryMonitor, Candidate, DataEnvelope, ExitAdvice, FinancialSearchResult, FinancialSearchStatus, KlineBar, KlineQueryOptions, MarketPrefetchResponse, MarketServiceStatus, ModelSettings, ModelSettingsDraft, Notification, NotificationSummary, PersonalArchiveJob, Portfolio, Quote, Report, ReportExecutionStatus, ReportSymbol, ResearchSettings, ResearchSubmission, Run, RunActivityResponse, Score, Snapshot, SystemResources, SystemSettings, SystemSettingsDraft, SystemSettingsUnlock, TokenPair, TradePlan, User } from './types'

const API_BASE = (import.meta.env.VITE_API_BASE || '/api/v1').replace(/\/$/, '')

export class ApiError extends Error {
  status: number
  detail: string
  code?: string

  constructor(status: number, detail: string, code?: string) {
    super(detail)
    this.status = status
    this.detail = detail
    this.code = code
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
    const rawDetail = typeof payload === 'object' && payload ? payload.detail || payload.message : payload
    const code = rawDetail && typeof rawDetail === 'object' ? rawDetail.code : undefined
    const detail = rawDetail && typeof rawDetail === 'object' ? rawDetail.message || code : rawDetail
    throw new ApiError(response.status, detail || `请求失败 (${response.status})`, code)
  }
  return payload as T
}

function params(values: Record<string, string | number | undefined>) {
  const query = new URLSearchParams()
  Object.entries(values).forEach(([key, value]) => value !== undefined && query.set(key, String(value)))
  const encoded = query.toString()
  return encoded ? `?${encoded}` : ''
}

type RawQuote = Quote & {
  change_percent?: number
  previous_close?: number
}

function normalizeQuote(row: RawQuote): Quote {
  return {
    ...row,
    price: row.price ?? 0,
    change_pct: row.change_pct ?? row.change_percent ?? 0,
    prev_close: row.prev_close ?? row.previous_close,
    source: row.source || row.status?.source,
    collected_at: row.collected_at || row.status?.collected_at,
    cached_at: row.cached_at || row.status?.cached_at,
    delayed: row.delayed ?? row.status?.delayed,
    is_delayed: row.is_delayed ?? row.status?.delayed,
  }
}

export const api = {
  login: (username: string, password: string) => request<User>('/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) }),
  token: (username: string, password: string) => request<TokenPair>('/auth/token', { method: 'POST', body: JSON.stringify({ username, password }) }),
  refreshToken: (refreshToken: string) => request<TokenPair>('/auth/refresh', { method: 'POST', body: JSON.stringify({ refresh_token: refreshToken }) }),
  revokeToken: (refreshToken: string) => request<void>('/auth/revoke', { method: 'POST', body: JSON.stringify({ refresh_token: refreshToken }) }),
  logout: () => request<void>('/auth/logout', { method: 'POST' }),
  me: () => request<User>('/auth/me'),
  assets: () => request<AssetState>('/assets'),
  saveAssets: (payload: AssetState) => request<AssetState>('/assets', { method: 'PUT', body: JSON.stringify(payload) }),
  saveExitMonitorSettings: (payload: Pick<AssetState, 'exit_monitor_enabled' | 'default_profit_trigger' | 'stop_loss_monitor_enabled' | 'buy_monitor_enabled'>, idempotencyKey = crypto.randomUUID()) => request<AssetState>('/assets/exit-monitor', { method: 'PUT', headers: { 'Idempotency-Key': idempotencyKey }, body: JSON.stringify(payload) }),
  saveMarketRefreshSettings: (marketRefreshIntervalSeconds: 15 | 30 | 60 | 120, idempotencyKey = crypto.randomUUID()) => request<AssetState>('/assets/market-refresh', { method: 'PUT', headers: { 'Idempotency-Key': idempotencyKey }, body: JSON.stringify({ market_refresh_interval_seconds: marketRefreshIntervalSeconds }) }),
  exitAdvice: (limit = 30) => request<ExitAdvice[]>(`/exit-advice${params({ limit })}`),
  submitManualExitAdvice: (symbol: string, idempotencyKey = crypto.randomUUID()) => request<ExitAdvice>('/exit-advice/manual', { method: 'POST', headers: { 'Idempotency-Key': idempotencyKey }, body: JSON.stringify({ symbol }) }),
  buyEntryMonitors: (limit = 50) => request<BuyEntryMonitor[]>(`/buy-entry-monitors${params({ limit })}`),
  updateBuyEntryMonitor: (symbol: string, enabled: boolean, idempotencyKey = crypto.randomUUID()) => request<BuyEntryMonitor[]>('/buy-entry-monitors', { method: 'PUT', headers: { 'Idempotency-Key': idempotencyKey }, body: JSON.stringify({ symbol, enabled }) }),
  notificationSummary: () => request<NotificationSummary>('/notifications/summary'),
  notifications: (options: { cursor?: string; unreadOnly?: boolean; limit?: number } = {}) => request<{ items: Notification[]; next_cursor?: string | null }>(`/notifications${params({ cursor: options.cursor, unread_only: options.unreadOnly ? 'true' : undefined, limit: options.limit })}`),
  markNotificationsRead: (notificationIds: string[], idempotencyKey = crypto.randomUUID()) => request<NotificationSummary>('/notifications/read', { method: 'POST', headers: { 'Idempotency-Key': idempotencyKey }, body: JSON.stringify({ notification_ids: notificationIds }) }),
  markAllNotificationsRead: (idempotencyKey = crypto.randomUUID()) => request<NotificationSummary>('/notifications/read-all', { method: 'POST', headers: { 'Idempotency-Key': idempotencyKey } }),
  aiModels: () => request<{ models: string[]; reasoning_efforts: string[]; web_search_available: boolean; cache_enabled: boolean }>('/ai/models'),
  aiCostSummary: (options: { days?: number; limit?: number; before?: string; threadId?: string } = {}) => request<AICostSummary>(`/ai/costs${params({ days: options.days, limit: options.limit, before: options.before, thread_id: options.threadId })}`),
  aiChatThreads: () => request<AIChatThread[]>('/ai/chat/threads'),
  aiChatThreadIndex: (options: { cursor?: string; q?: string; archived?: boolean; limit?: number } = {}) => request<{ items: AIChatThread[]; next_cursor?: string | null }>(`/ai/chat/thread-index${params({ cursor: options.cursor, q: options.q, archived: options.archived ? 'true' : undefined, limit: options.limit })}`),
  createAIChatThread: (title = '新对话') => request<AIChatThread>('/ai/chat/threads', { method: 'POST', body: JSON.stringify({ title }) }),
  patchAIChatThread: (threadId: string, payload: { title?: string; pinned?: boolean; archived?: boolean; group_label?: string | null }) => request<AIChatThread>(`/ai/chat/threads/${encodeURIComponent(threadId)}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  deleteAIChatThread: (threadId: string) => request<void>(`/ai/chat/threads/${encodeURIComponent(threadId)}`, { method: 'DELETE' }),
  bulkDeleteAIChatThreads: (threadIds: string[]) => request<{ deleted: number }>('/ai/chat/threads:bulk-delete', { method: 'POST', body: JSON.stringify({ thread_ids: threadIds }) }),
  aiChatMessages: (threadId: string) => request<AIChatMessage[]>(`/ai/chat/threads/${encodeURIComponent(threadId)}/messages`),
  uploadAIChatAttachments: (files: File[], threadId?: string) => {
    const body = new FormData()
    files.forEach((file) => body.append('files', file))
    if (threadId) body.append('thread_id', threadId)
    return request<AIChatAttachment[]>('/ai/chat/attachments', { method: 'POST', body })
  },
  createPersonalExport: (passphrase: string) => request<PersonalArchiveJob>('/me/data-exports', { method: 'POST', body: JSON.stringify({ passphrase }) }),
  personalExport: (archiveId: string) => request<PersonalArchiveJob>(`/me/data-exports/${encodeURIComponent(archiveId)}`),
  deletePersonalExport: (archiveId: string) => request<void>(`/me/data-exports/${encodeURIComponent(archiveId)}`, { method: 'DELETE' }),
  uploadPersonalImport: (file: File, passphrase: string) => {
    const body = new FormData(); body.append('archive', file); body.append('passphrase', passphrase)
    return request<PersonalArchiveJob>('/me/data-imports', { method: 'POST', body })
  },
  personalImport: (archiveId: string) => request<PersonalArchiveJob>(`/me/data-imports/${encodeURIComponent(archiveId)}`),
  applyPersonalImport: (archiveId: string, mergeOptions: Record<string, unknown>, idempotencyKey: string) => request<PersonalArchiveJob>(`/me/data-imports/${encodeURIComponent(archiveId)}/apply`, { method: 'POST', headers: { 'Idempotency-Key': idempotencyKey }, body: JSON.stringify({ merge_options: mergeOptions }) }),

  quote: async (symbol: string, refresh = false) => normalizeQuote(await request<RawQuote>(`/market/quotes/${encodeURIComponent(symbol)}${params({ refresh: refresh ? 'true' : undefined })}`)),
  quotes: async (symbols: string[], refresh = false) => {
    const rows = await request<RawQuote[]>(`/market/quotes${params({ symbols: symbols.join(','), refresh: refresh ? 'true' : undefined })}`)
    return rows.map(normalizeQuote)
  },
  kline: async (symbol: string, period: string, limit = 160, options: KlineQueryOptions | boolean = {}) => {
    const query = typeof options === 'boolean' ? { refresh: options } : options
    const payload = await request<DataEnvelope<KlineBar[]> & { bars: KlineBar[] }>(`/market/klines/${encodeURIComponent(symbol)}${params({
      period,
      limit,
      adjust: 'hfq',
      start: query.start,
      end: query.end,
      refresh: query.refresh ? 'true' : undefined,
    })}`)
    return { ...payload, bars: (payload.bars || []).map((bar) => ({ ...bar, time: bar.time || bar.timestamp })) }
  },
  prefetchMarket: async (symbols: string[], periods = ['day'], limit = 160, includeQuotes = true) => {
    const payload = await request<MarketPrefetchResponse>('/market/prefetch', {
      method: 'POST',
      body: JSON.stringify({ symbols, periods, limit, include_quotes: includeQuotes }),
    })
    if (!payload || Array.isArray(payload)) return { quotes: [], klines: {}, errors: {} } as MarketPrefetchResponse
    return {
      ...payload,
      quotes: (payload.quotes || []).map((row) => normalizeQuote(row as RawQuote)),
      klines: payload.klines || {},
      errors: payload.errors || {},
    }
  },
  marketStatus: () => request<MarketServiceStatus>('/market/status'),
  financialSearch: (query: string) => request<FinancialSearchResult>(`/search/financial${params({ q: query })}`),
  financialSearchStatus: () => request<FinancialSearchStatus>('/search/status'),

  scores: (date: string, runId?: string) => request<Score[]>(`/scores/${date}${params({ run_id: runId })}`),
  score: (date: string, symbol: string, runId?: string) => request<Score>(`/scores/${date}/${encodeURIComponent(symbol)}${params({ run_id: runId })}`),
  scoreLineage: (date: string, symbol: string, runId?: string) => request<Record<string, unknown>>(`/scores/${date}/${encodeURIComponent(symbol)}/lineage${params({ run_id: runId })}`),
  candidates: (date: string, runId?: string) => request<Candidate[]>(`/candidates/${date}${params({ run_id: runId })}`),
  portfolio: (date: string, runId?: string) => request<Portfolio>(`/portfolios/${date}${params({ run_id: runId })}`),
  report: (date: string, runId?: string) => request<Report>(`/reports/${date}${params({ run_id: runId })}`),
  reportContent: (reportId: string) => request<{ content?: string; body?: string }>(`/reports/${reportId}/content`),
  reportSymbols: (reportId: string) => request<ReportSymbol[]>(`/reports/${reportId}/symbols`),
  reportExecutionStatus: (reportId: string) => request<ReportExecutionStatus>(`/reports/${reportId}/execution-status`),
  reportTradePlans: (reportId: string) => request<TradePlan[]>(`/reports/${reportId}/trade-plans`),
  submitTradePlan: (reportId: string, payload: { symbols: string[]; budget_override?: number; objective?: 'RISK_ADJUSTED_RETURN' }, idempotencyKey = crypto.randomUUID()) => request<TradePlan>(`/reports/${reportId}/trade-plans`, { method: 'POST', headers: { 'Idempotency-Key': idempotencyKey }, body: JSON.stringify(payload) }),
  tradePlan: (planId: string) => request<TradePlan>(`/trade-plans/${planId}`),

  submitResearch: (payload: string | ResearchSubmission) => request<Run>('/research/runs', { method: 'POST', body: JSON.stringify(typeof payload === 'string' ? { trading_date: payload } : payload) }),
  researchSettings: () => request<ResearchSettings>('/research/settings'),
  saveResearchSettings: (payload: boolean | { automatic_reports: import('./types').AutomaticResearchReportSettings[] }) => request<ResearchSettings>('/research/settings', { method: 'PUT', body: JSON.stringify(typeof payload === 'boolean' ? { auto_enabled: payload } : payload) }),
  researchRuns: (limit = 5, tradingDate?: string, published = false) => request<Run[]>(`/research/runs${params({ limit, trading_date: tradingDate, mine: 'true', published: published ? 'true' : undefined })}`),
  cancelResearch: (runId: string) => request<Run>(`/research/runs/${encodeURIComponent(runId)}/cancel`, { method: 'POST' }),
  runs: () => request<Run[] | { items: Run[] }>('/runs'),
  runActivity: (options: { cursor?: string; type?: string; status?: string; limit?: number } = {}) => request<RunActivityResponse>(`/runs/activity${params({ cursor: options.cursor, type: options.type, status: options.status, limit: options.limit })}`),
  run: (runId: string) => request<Run>(`/runs/${runId}`),
  audit: (runId: string) => request<AuditEvent[]>(`/runs/${runId}/audit`),
  submitBacktest: (payload: { name: string; start_date: string; end_date: string; snapshot_ids: string[]; config: Record<string, unknown> }) => request<Run>('/backtests', { method: 'POST', body: JSON.stringify(payload) }),
  backtest: (backtestId: string) => request<Run>(`/backtests/${backtestId}`),
  backtests: () => request<Run[] | { items: Run[] }>('/backtests'),
  retryBacktest: (backtestId: string) => request<Run>(`/backtests/${backtestId}/retry`, { method: 'POST' }),
  snapshots: () => request<Snapshot[]>('/snapshots?dataset=backtest_bundle'),

  users: () => request<User[]>('/admin/users'),
  createUser: (payload: { username: string; password: string; role: string }) => request<User>('/admin/users', { method: 'POST', body: JSON.stringify({ ...payload, role: payload.role.toUpperCase() }) }),
  setUserDisabled: (id: string, disabled: boolean) => request<User>(`/admin/users/${id}`, { method: 'PATCH', body: JSON.stringify({ enabled: !disabled }) }),
  resetPassword: (id: string, password: string) => request<User>(`/admin/users/${id}`, { method: 'PATCH', body: JSON.stringify({ password }) }),
  modelSettings: () => request<ModelSettings>('/admin/model-settings'),
  testModelSettings: (payload: ModelSettingsDraft) => request<{ reachable: boolean; message: string; model: string; checked_at: string }>('/admin/model-settings/test', { method: 'POST', body: JSON.stringify(payload) }),
  saveModelSettings: (payload: ModelSettingsDraft) => request<ModelSettings>('/admin/model-settings', { method: 'PUT', body: JSON.stringify(payload) }),
  listModels: (payload: ModelSettingsDraft) => request<{ models: string[] }>('/admin/model-settings/models', { method: 'POST', body: JSON.stringify(payload) }),
  systemSettings: () => request<SystemSettings>('/admin/system-settings'),
  systemResources: () => request<SystemResources>('/admin/system-resources'),
  unlockSystemSettings: (password: string) => request<SystemSettingsUnlock>('/admin/system-settings/unlock', { method: 'POST', body: JSON.stringify({ password }) }),
  saveSystemSettings: (payload: SystemSettingsDraft, unlockToken: string, idempotencyKey: string = crypto.randomUUID()) => request<SystemSettings>('/admin/system-settings', { method: 'PUT', headers: { 'Idempotency-Key': idempotencyKey, 'X-System-Settings-Unlock': unlockToken }, body: JSON.stringify(payload) }),
  restoreSystemSetting: (field: string, unlockToken: string) => request<SystemSettings>(`/admin/system-settings/${encodeURIComponent(field)}`, { method: 'DELETE', headers: { 'X-System-Settings-Unlock': unlockToken } }),
  restoreAllSystemSettings: (unlockToken: string) => request<SystemSettings>('/admin/system-settings', { method: 'DELETE', headers: { 'X-System-Settings-Unlock': unlockToken } }),
}

export async function streamAIChat(
  threadId: string,
  payload: { content: string; model: string; reasoning_effort: string; web_search: boolean; attachment_ids?: string[]; mention_refs?: Array<{ symbol: string; name: string }>; decision_at?: string },
  onEvent: (event: Record<string, unknown>) => void,
  signal?: AbortSignal,
  idempotencyKey: string = crypto.randomUUID(),
) {
  const headers = new Headers({ 'Content-Type': 'application/json' })
  headers.set('Idempotency-Key', idempotencyKey)
  const token = csrfToken()
  if (token) { headers.set('X-CSRF-Token', token); headers.set('X-CSRFToken', token) }
  let sawDelta = false
  for (let attempt = 0; attempt <= 2; attempt += 1) {
    try {
      const response = await fetch(`${API_BASE}/ai/chat/threads/${encodeURIComponent(threadId)}/messages:stream`, {
        method: 'POST', headers, body: JSON.stringify(payload), credentials: 'include', signal,
      })
      if (!response.ok || !response.body) {
        const error = new ApiError(response.status, `AI 对话请求失败 (${response.status})`)
        ;(error as ApiError & { retryable?: boolean }).retryable = [408, 429, 500, 502, 503, 504].includes(response.status)
        throw error
      }
      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      let terminalDone = false
      while (true) {
        const { value, done } = await reader.read()
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done })
        const blocks = buffer.split('\n\n')
        buffer = blocks.pop() || ''
        for (const block of blocks) {
          const data = block.split('\n').filter((line) => line.startsWith('data:')).map((line) => line.slice(5).trim()).join('\n')
          if (!data) continue
          const event = JSON.parse(data) as Record<string, unknown>
          if (event.type === 'delta') sawDelta = true
          if (event.type === 'done') terminalDone = true
          onEvent(event)
          if (event.type === 'error') {
            const error = new ApiError(0, String(event.message || 'AI 对话生成失败'), String(event.code || 'CHAT_ERROR'))
            ;(error as ApiError & { requestId?: string; retryable?: boolean }).requestId = String(event.request_id || '')
            ;(error as ApiError & { requestId?: string; retryable?: boolean }).retryable = Boolean(event.retryable)
            throw error
          }
        }
        if (done) break
      }
      if (!terminalDone) {
        const error = new ApiError(0, 'AI 对话连接在完成前中断', 'CHAT_STREAM_INCOMPLETE')
        ;(error as ApiError & { retryable?: boolean }).retryable = !sawDelta
        throw error
      }
      return
    } catch (reason) {
      if (signal?.aborted) throw reason
      const retryable = !sawDelta && (
        reason instanceof TypeError
        || (reason instanceof ApiError && (
          [408, 429, 500, 502, 503, 504].includes(reason.status)
          || Boolean((reason as ApiError & { retryable?: boolean }).retryable)
        ))
      )
      if (!retryable || attempt >= 2) throw reason
      await new Promise((resolve) => window.setTimeout(resolve, 250 * (2 ** attempt)))
    }
  }
}

export async function downloadPersonalArchive(archiveId: string) {
  const response = await fetch(`${API_BASE}/me/data-exports/${encodeURIComponent(archiveId)}/download`, { credentials: 'include' })
  if (!response.ok) throw new ApiError(response.status, `档案下载失败 (${response.status})`)
  return response.blob()
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
