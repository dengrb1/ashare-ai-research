export type Role = 'admin' | 'researcher' | 'user'

export interface User {
  id?: string
  user_id?: string
  username: string
  role: Role | string
  is_active?: boolean
  enabled?: boolean
  disabled?: boolean
  created_at?: string
}

export interface Quote {
  symbol: string
  name?: string
  price: number
  change?: number
  change_pct: number
  open?: number
  high?: number
  low?: number
  prev_close?: number
  volume?: number
  amount?: number
  source?: string
  collected_at?: string
  cached_at?: string
  is_delayed?: boolean
  delayed?: boolean
  status?: MarketDataStatus
}

export interface MarketDataStatus {
  source: string
  collected_at: string
  cached_at: string
  delayed: boolean
  stale?: boolean
  message?: string | null
}

export interface Snapshot {
  snapshot_id: string
  dataset: string
  source: string
  fetched_at: string
  row_count: number
  status: string
  details?: Record<string, unknown>
}

export interface FinancialSearchResult {
  query: string
  provider: string
  upstream: string
  mode: 'cli' | 'embedded'
  searched_at: string
  elapsed_ms: number
  entities: Array<{ name: string; code: string; [key: string]: unknown }>
  recalls: Array<{ type: string; desc: string; content: string; [key: string]: unknown }>
  raw_sha256: string
  live_data_isolated_from_snapshots: boolean
}

export interface FinancialSearchStatus {
  provider: string
  upstream: string
  mode: 'cli' | 'embedded'
  available: boolean
  script_path?: string | null
  message: string
  live_data_isolated_from_snapshots: boolean
}

export interface KlineBar {
  time?: string
  trading_date?: string
  datetime?: string
  timestamp?: string
  open: number
  high: number
  low: number
  close: number
  volume: number
  amount?: number
}

export interface KlinePayload {
  symbol: string
  period: string
  adjustment: string
  bars: KlineBar[]
  status: MarketDataStatus
}

export interface MarketPrefetchResponse {
  quotes: Quote[]
  klines: Record<string, Record<string, KlinePayload>>
  errors: Record<string, string>
}

export interface DataEnvelope<T> {
  data?: T
  items?: T
  quotes?: T
  bars?: T
  status?: MarketDataStatus
  source?: string
  collected_at?: string
  cached_at?: string
  is_delayed?: boolean
  delayed?: boolean
}

export interface Run {
  run_id: string
  backtest_id?: string
  run_type?: string
  trading_date?: string
  decision_at?: string
  status: string
  started_at?: string
  created_at?: string
  completed_at?: string | null
  error_message?: string | null
  result?: Record<string, unknown> | null
  metrics?: Record<string, unknown> | null
  artifacts?: Record<string, unknown> | null
  name?: string
  start_date?: string
  end_date?: string
}

export interface Score {
  symbol: string
  total_score: number
  fundamental_score?: number
  technical_score?: number
  sentiment_score?: number
  quality_confidence_score?: number
  formula_version?: string
}

export interface Candidate {
  symbol: string
  rank: number
  total_score: number
  prediction_percentile?: number
  industry_code?: string
  event_risk_multiplier?: number
}

export interface Portfolio {
  portfolio_id: string
  run_id: string
  trading_date: string
  effective_trading_date?: string
  status: string
  expected_turnover?: number
  cash_weight?: number
  positions: Array<Record<string, unknown>>
  rejection_reasons?: string[]
}

export interface AuditEvent {
  event_id: string
  run_id: string
  event_type: string
  severity: string
  message: string
  details?: Record<string, unknown>
  created_at: string
}

export interface Report {
  report_id: string
  run_id: string
  trading_date: string
  report_type: string
  content?: string
  body?: string
  object_uri?: string
  created_at?: string
}
