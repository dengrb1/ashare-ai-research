export type Role = 'admin' | 'researcher' | 'user'

export type ResearchExecutionMode = 'SERIAL' | 'DUAL'

export interface WorkerHealth {
  worker_id: string
  role: string
  healthy: boolean
  loaded_mode: ResearchExecutionMode | 'UNKNOWN'
  topology_sha256?: string | null
  last_heartbeat_at?: string | null
}

export interface QueueSummary {
  pending: number
  processing: number
}

export interface SystemSettings {
  configuration_id: string | null
  version: number
  config_sha256: string
  source: 'database' | 'environment'
  values: Record<string, string | number | boolean | null>
  sources: Record<string, 'database' | 'environment'>
  secret_configured: Record<string, boolean>
  secret_sources: Record<string, 'database' | 'environment'>
  read_only_environment: Record<string, string | number | boolean | null>
  topology_sha256: string
  actual_loaded_mode: ResearchExecutionMode | 'UNKNOWN'
  restart_required: boolean
  workers: WorkerHealth[]
  queues: Record<string, QueueSummary>
  compose_restart_command: string
}

export type SystemSettingsDraft = Partial<Record<string, string | number | boolean | null>>

export interface SystemResourceUsage {
  total_bytes: number
  used_bytes: number
  available_bytes: number
  percent: number
}

export interface SystemServiceResource {
  service_id: string
  role: string
  healthy: boolean
  memory_used_bytes?: number | null
  memory_cache_bytes?: number | null
  memory_limit_bytes?: number | null
  cpu_percent?: number | null
  collected_at?: string | null
}

export interface SystemResources {
  collected_at: string
  scope: 'HOST' | 'CONTAINER'
  scope_label: string
  memory: SystemResourceUsage
  cpu: { percent: number; logical_cores: number }
  disk: SystemResourceUsage
  services: SystemServiceResource[]
  topology_estimate: {
    worker_replicas: number
    typical_per_worker_bytes: number
    typical_increment_bytes: number
    maximum_increment_bytes: number
    projected_available_bytes: number
    level: 'NORMAL' | 'WARNING' | 'CRITICAL'
    messages: string[]
  }
  level: 'NORMAL' | 'WARNING' | 'CRITICAL'
  warnings: string[]
}

export interface SystemSettingsUnlock {
  unlock_token: string
  expires_at: string
}

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

export interface TokenPair {
  access_token: string
  token_type: 'bearer'
  expires_in: number
  refresh_token: string
  refresh_expires_in: number
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

export interface PaperPosition {
  symbol: string
  name: string
  quantity: number
  cost: number
  /** Legacy manual target, kept so older saved records remain readable. */
  target_weight?: number | null
  acquired_on?: string | null
  profit_trigger_amount?: number | string | null
  exit_trigger_price?: number | string | null
  stop_loss_price?: number | string | null
  stop_loss_mode?: 'AUTO_ATR20' | 'MANUAL' | 'FALLBACK_8PCT'
  stop_loss_enabled?: boolean
}

export interface AssetState {
  watchlist: string[]
  positions: PaperPosition[]
  total_assets?: number | null
  exit_monitor_enabled?: boolean
  default_profit_trigger?: number | string | null
  stop_loss_monitor_enabled?: boolean
  buy_monitor_enabled?: boolean
  market_refresh_interval_seconds?: 15 | 30 | 60 | 120
  updated_at?: string | null
}

export interface ExitAdvice {
  advice_id: string
  operation_run_id?: string | null
  symbol: string
  status: string
  action?: 'HOLD' | 'REDUCE' | 'SELL' | null
  decision_at: string
  available_at: string
  current_price: number | string
  unrealized_profit: number | string
  trigger_amount: number | string
  trigger_type?: 'PRICE' | 'PROFIT_AMOUNT' | 'MANUAL' | 'STOP_LOSS'
  trigger_price?: number | string | null
  position_snapshot: Record<string, unknown>
  research_context: Record<string, unknown>
  result?: Record<string, unknown> | null
  model_name?: string | null
  reasoning_effort?: string | null
  cache_hit: boolean
  created_at: string
  completed_at?: string | null
  error_message?: string | null
  status_url?: string | null
}

export interface TradeAdviceMonitor {
  monitor_id: string
  symbol: string
  enabled: boolean
  manual_buy_price?: number | string | null
  manual_sell_price?: number | string | null
  ai_buy_price?: number | string | null
  ai_sell_price?: number | string | null
  stop_loss_price?: number | string | null
  rationale: Record<string, unknown>
  generated_for?: string | null
  generated_at?: string | null
  model_name?: string | null
  model_source?: string | null
  last_alert_at?: string | null
  last_alert_types: string[]
  error_code?: string | null
  created_at: string
  updated_at: string
}

export interface AIChatThread {
  thread_id: string
  title: string
  group_mode?: 'AUTO' | 'MANUAL'
  group_type?: 'GENERAL' | 'SINGLE' | 'MULTI'
  group_label?: string | null
  cumulative_mentions?: Array<{ symbol: string; name: string }>
  pinned_at?: string | null
  archived_at?: string | null
  created_at: string
  updated_at: string
}

export interface AIChatMessage {
  message_id: string
  thread_id: string
  role: 'user' | 'assistant'
  content: string
  status?: 'PENDING' | 'STREAMING' | 'COMPLETED' | 'FAILED' | 'CANCELLED'
  trading_date?: string
  decision_at?: string
  available_at?: string
  parent_message_id?: string | null
  mentioned_symbols: string[]
  mention_refs?: Array<{ symbol: string; name: string }>
  attachment_ids?: string[]
  model_name?: string | null
  reasoning_effort?: string | null
  sources: Array<Record<string, unknown>>
  cache_hit: boolean
  input_tokens: number
  cached_input_tokens?: number
  cache_write_tokens?: number
  output_tokens: number
  reasoning_tokens?: number
  cache_policy?: 'GROK' | 'OPENAI' | 'COMPATIBLE'
  context_budget_status?: 'WITHIN_BUDGET' | 'HISTORY_TRIMMED' | 'COMPACTED' | 'CONTEXT_TOO_LARGE'
  error_code?: string | null
  request_id?: string | null
  streaming_mode?: 'STREAMING' | 'DEGRADED' | 'CACHED'
  data_status?: Record<string, unknown>
  response_id?: string | null
  created_at: string
}

export interface Notification {
  notification_id: string
  notification_type: string
  severity: 'INFO' | 'WARNING' | 'HIGH' | 'CRITICAL'
  title: string
  body: string
  resource_type?: string | null
  resource_id?: string | null
  payload: Record<string, unknown>
  read_at?: string | null
  created_at: string
  expires_at: string
}

export interface NotificationSummary {
  unread_count: number
  high_risk_unread_count: number
  latest: Notification[]
}

export interface BuyEntryMonitor {
  monitor_id: string
  symbol: string
  status: string
  effective_date: string
  expires_at: string
  entry_low: number | string
  entry_high: number | string
  score_run_id?: string | null
  trade_plan_id?: string | null
  rationale: Record<string, unknown>
  triggered_at?: string | null
  error_code?: string | null
  created_at: string
  updated_at: string
}

export interface AIChatAttachment {
  attachment_id: string
  thread_id?: string | null
  mime_type: string
  byte_size: number
  width: number
  height: number
  uploaded_at: string
  expires_at: string
  deleted_at?: string | null
  deletion_reason?: string | null
}

export interface PersonalArchiveJob {
  archive_id: string
  kind: 'EXPORT' | 'IMPORT_PREVIEW' | 'IMPORT_APPLY'
  status: 'PENDING' | 'PROCESSING' | 'SUCCEEDED' | 'FAILED' | 'CANCELLED'
  phase: string
  progress: number
  source_archive_id?: string | null
  result?: Record<string, unknown> | null
  error_code?: string | null
  created_at: string
  started_at?: string | null
  completed_at?: string | null
  expires_at: string
  deleted_at?: string | null
}

export interface MarketDataStatus {
  source: string
  collected_at: string
  cached_at: string
  delayed: boolean
  stale?: boolean
  message?: string | null
}

export type MarketSessionState = 'OPEN' | 'PRE_OPEN' | 'BREAK' | 'CLOSED' | 'UNKNOWN'

export interface MarketSessionStatus {
  state: MarketSessionState
  as_of: string
  trading_date: string
  is_trading_day: boolean | null
  reason: 'TRADING_SESSION' | 'BEFORE_OPEN' | 'MIDDAY_BREAK' | 'AFTER_CLOSE' | 'NON_TRADING_DAY' | 'CALENDAR_UNAVAILABLE'
}

export interface MarketServiceStatus {
  market_session?: MarketSessionStatus
  [key: string]: unknown
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
  mode: 'cli' | 'embedded' | 'direct' | 'ai'
  searched_at: string
  elapsed_ms: number
  entities: Array<{ name: string; code: string; [key: string]: unknown }>
  recalls: Array<{ type: string; desc: string; content: string; [key: string]: unknown }>
  raw_sha256: string
  outcome?: Record<string, unknown>
  interpretation?: string
  sources?: Array<{ source: string; uri?: string; fetched_at?: string; report_date?: string | null; notice_date?: string | null }>
  warnings?: string[]
  live_data_isolated_from_snapshots: boolean
}

export interface FinancialSearchStatus {
  provider: string
  upstream: string
  mode: 'cli' | 'embedded' | 'direct' | 'ai'
  available: boolean
  configured?: boolean
  reachable?: boolean
  degraded?: boolean
  model?: string | null
  script_path?: string | null
  message: string
  live_data_isolated_from_snapshots: boolean
}

export interface ModelSettings {
  configuration_id: string | null
  version: number
  config_sha256: string
  source: string
  provider: string
  base_url: string
  api_key_configured: boolean
  search_model: string
  search_reasoning_effort: string
  research_model: string
  research_reasoning_effort: string
  model_profiles: ModelProfile[]
  timeout_seconds: number
  enabled: boolean
  configured: boolean
  reachable: boolean
  degraded: boolean
  status_message: string
  checked_at?: string | null
}

export interface ModelSettingsDraft {
  base_url: string
  api_key?: string
  search_model: string
  search_reasoning_effort: string
  research_model: string
  research_reasoning_effort: string
  model_profiles: ModelProfile[]
  timeout_seconds: number
  enabled: boolean
}

export interface ModelProfile {
  model: string
  cache_policy: 'GROK' | 'OPENAI' | 'COMPATIBLE'
  context_window_tokens: number
  output_token_reserve: number
  reasoning_token_reserve: number
  input_price_per_million: number | string
  cached_input_price_per_million: number | string
  cache_write_price_per_million: number | string
  output_price_per_million: number | string
}

export interface AICostValue {
  requests: number
  cache_hits: number
  input_tokens: number
  cached_input_tokens: number
  cache_write_tokens: number
  uncached_input_tokens: number
  output_tokens: number
  estimated_spend_usd: number | string
  estimated_savings_usd: number | string
}

export interface AICostSummary {
  days: number
  items: Array<AICostValue & { bucket_date: string }>
  next_cursor?: string | null
  totals: AICostValue
  current_turn?: Omit<AICostValue, 'cache_hits'> & { cache_hit: boolean } | null
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
  turnover_rate?: number | null
}

export type KlineRange = '1d' | '5d' | '1m' | '3m' | '6m' | '1y'

export interface KlineQueryOptions {
  start?: string
  end?: string
  refresh?: boolean
}

export interface KlineLoadOptions extends KlineQueryOptions {
  limit?: number
  range: KlineRange
  chunk?: string
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
  phase?: string
  progress?: number
  report_id?: string | null
  report_type?: string | null
  report_created_at?: string | null
  retry_count?: number
  research_scope?: ResearchScope
  target_symbols?: string[]
  total_budget?: number | string | null
  per_symbol_budget?: number | string | null
  max_stock_price?: number | string | null
  portfolio_requested?: boolean
  portfolio_generated?: boolean
  reason_code?: string | null
  reason_message?: string | null
  formal_eligible_count?: number | null
  excluded_symbol_count?: number
  portfolio_reason_code?: string | null
  portfolio_reason_message?: string | null
  trigger_source?: 'AUTO' | 'MANUAL'
  automatic_report_slot?: 'A' | 'B' | null
  requested_date?: string | null
  reused?: boolean
  data_readiness_state?: string | null
  next_retry_at?: string | null
}

export interface RunActivity extends Run {
  resource_type: 'RESEARCH' | 'BACKTEST' | 'TRADE_PLAN' | 'EXIT_ADVICE'
  resource_id?: string | null
  resource_url?: string | null
  title?: string | null
  symbol?: string | null
}

export interface RunActivityResponse {
  items: RunActivity[]
  next_cursor?: string | null
}

export type ResearchScope = 'MARKET' | 'WATCHLIST' | 'CUSTOM'

export interface ResearchSubmission {
  trading_date: string
  scope: ResearchScope
  symbols?: string[]
  total_budget?: number
  per_symbol_budget?: number
  max_stock_price?: number
}

export interface ResearchSettings {
  auto_enabled: boolean
  updated_at?: string | null
  automatic_scope: ResearchScope
  automatic_total_budget: number | string
  automatic_per_symbol_budget: number | string
  automatic_max_stock_price?: number | string | null
  automatic_reports?: AutomaticResearchReportSettings[]
  schedule_timezone: 'Asia/Shanghai'
  schedule_time: '15:05'
  snapshot_mode: 'SYSTEM_ENFORCED'
  portfolio_target_count: number
}

export interface AutomaticResearchReportSettings {
  slot: 'A' | 'B'
  enabled: boolean
  scope: ResearchScope
  symbols: string[]
  total_budget: number | string
  per_symbol_budget: number | string
  max_stock_price?: number | string | null
  config_version?: number
}

export interface Score {
  symbol: string
  total_score: number
  fundamental_score?: number
  technical_score?: number
  sentiment_score?: number
  quality_confidence_score?: number
  base_total_score?: number
  dividend_bonus?: number
  event_risk_multiplier?: number
  formula_version?: string
  prediction_percentile?: number
  rank?: number
}

export interface Candidate {
  symbol: string
  name?: string | null
  rank: number
  total_score: number
  base_total_score?: number | null
  dividend_bonus?: number
  prediction_percentile?: number
  industry_code?: string
  industry_name?: string | null
  event_risk_multiplier?: number
}

export interface ReportSymbol {
  symbol: string
  name?: string | null
  research_status: 'FORMAL' | 'FORMAL_WITH_LIMITATIONS' | 'RISK_BLOCKED'
  advice_eligible: boolean
  recommendation?: 'NO_BUY' | null
  exclusion_reasons: string[]
  data_quality: Record<string, unknown>
  score: Score
  rank?: number | null
  prediction_percentile?: number | null
  industry_code?: string | null
  industry_name?: string | null
  plain_language_summary?: string | null
  component_summaries?: Record<string, string>
}

export interface ReportExecutionStatus {
  report_id: string
  as_of: string
  items: Array<{
    symbol: string
    held_quantity: number
    acquired_on?: string | null
    sellable_quantity: number
    t1_restricted: boolean
    blockers: string[]
  }>
}

export interface Portfolio {
  portfolio_id?: string | null
  run_id: string
  trading_date: string
  effective_trading_date?: string
  status: string
  expected_turnover?: number
  cash_weight?: number
  positions: Array<Record<string, unknown>>
  rejection_reasons?: string[]
  observation_only?: boolean
  research_only?: boolean
  message?: string | null
  reason_code?: string | null
  formal_eligible_symbols?: string[]
  excluded_symbols?: Record<string, string[]>
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

export interface TradePlan {
  plan_id: string
  user_id: string
  report_id: string
  run_id: string
  operation_run_id?: string | null
  trading_date: string
  decision_at: string
  available_at: string
  status: string
  objective: 'RISK_ADJUSTED_RETURN' | string
  symbols: string[]
  budget_override?: number | string | null
  snapshot_ids: string[]
  optimizer_version: string
  config_version: string
  prompt_version?: string | null
  deterministic_result?: Record<string, unknown> | null
  ai_explanation?: Record<string, unknown> | null
  input_hash: string
  output_hash?: string | null
  object_uri?: string | null
  object_sha256?: string | null
  created_at: string
  started_at?: string | null
  completed_at?: string | null
  error_message?: string | null
}
