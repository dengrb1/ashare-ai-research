import type { KlineBar, KlineRange } from './types'

export const KLINE_PERIODS = [
  { value: '1m', label: '1 分钟' },
  { value: '5m', label: '5 分钟' },
  { value: '15m', label: '15 分钟' },
  { value: '30m', label: '30 分钟' },
  { value: '60m', label: '60 分钟' },
  { value: 'day', label: '日线' },
] as const

export const KLINE_RANGES: Array<{ value: KlineRange; label: string }> = [
  { value: '1d', label: '近 1 日' },
  { value: '5d', label: '近 5 日' },
  { value: '1m', label: '近 1 月' },
  { value: '3m', label: '近 3 月' },
  { value: '6m', label: '近 6 月' },
  { value: '1y', label: '近 1 年' },
]

const SHANGHAI_PARTS = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
})
const SHANGHAI_DAY = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
})
const DAY_MS = 24 * 60 * 60 * 1000

export interface KlineRangePlan {
  range: KlineRange
  label: string
  start: string
  end: string
  startMs: number
  endMs: number
  tradingDays?: number
}

export interface KlineChunkWindow {
  start: string
  end: string
  key: string
}

function shanghaiWallTime(date: Date) {
  const parts = Object.fromEntries(SHANGHAI_PARTS.formatToParts(date).map((part) => [part.type, part.value]))
  return Date.UTC(Number(parts.year), Number(parts.month) - 1, Number(parts.day), Number(parts.hour), Number(parts.minute), Number(parts.second))
}

function formatWallTime(value: number) {
  const date = new Date(value)
  const pad = (part: number) => String(part).padStart(2, '0')
  return `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}T${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())}+08:00`
}

function subtractMonths(value: number, months: number) {
  const date = new Date(value)
  const originalDay = date.getUTCDate()
  const targetMonth = date.getUTCMonth() - months
  date.setUTCDate(1)
  date.setUTCMonth(targetMonth)
  const lastDay = new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth() + 1, 0)).getUTCDate()
  date.setUTCDate(Math.min(originalDay, lastDay))
  return date.getTime()
}

export function getKlineRangePlan(range: KlineRange, now = new Date()): KlineRangePlan {
  const endWall = shanghaiWallTime(now)
  const label = KLINE_RANGES.find((item) => item.value === range)?.label || range
  let startWall = endWall
  let tradingDays: number | undefined
  if (range === '1d') {
    startWall -= 14 * DAY_MS
    tradingDays = 1
  } else if (range === '5d') {
    startWall -= 30 * DAY_MS
    tradingDays = 5
  } else {
    startWall = subtractMonths(endWall, range === '1m' ? 1 : range === '3m' ? 3 : range === '6m' ? 6 : 12)
  }
  const start = formatWallTime(startWall)
  const end = formatWallTime(endWall)
  return { range, label, start, end, startMs: Date.parse(start), endMs: Date.parse(end), tradingDays }
}

function chunkDuration(period: string) {
  const durations: Record<string, number> = {
    '1m': 7 * DAY_MS,
    '5m': 30 * DAY_MS,
    '15m': 90 * DAY_MS,
    '30m': 180 * DAY_MS,
    '60m': 365 * DAY_MS,
  }
  return durations[period.toLowerCase()] || Number.POSITIVE_INFINITY
}

export function getKlineChunkWindow(period: string, plan: KlineRangePlan, before?: string): KlineChunkWindow | null {
  const anchor = before ? Date.parse(before) : plan.endMs
  if (!Number.isFinite(anchor) || anchor <= plan.startMs) return null
  const duration = chunkDuration(period)
  const startMs = period.toLowerCase() === 'day' ? plan.startMs : Math.max(plan.startMs, anchor - duration)
  const endMs = Math.min(plan.endMs, anchor)
  const start = new Date(startMs).toISOString()
  const end = new Date(endMs).toISOString()
  // The newest segment is a moving window. Give it a stable cache identity so
  // automatic refresh replaces it instead of accumulating one chunk per poll.
  return { start, end, key: before ? `${start}|${end}` : 'latest' }
}

export function barTimestamp(bar: KlineBar) {
  return bar.time || bar.timestamp || bar.datetime || bar.trading_date || ''
}

export function barTimeMs(bar: KlineBar) {
  const value = barTimestamp(bar)
  if (!value) return Number.NaN
  return Date.parse(/^\d{4}-\d{2}-\d{2}$/.test(value) ? `${value}T00:00:00+08:00` : value)
}

function finiteNumber(value: unknown) {
  const parsed = typeof value === 'number' ? value : typeof value === 'string' && value.trim() ? Number(value) : Number.NaN
  return Number.isFinite(parsed) ? parsed : null
}

function validKlineBar(bar: KlineBar): KlineBar | null {
  const open = finiteNumber(bar.open)
  const high = finiteNumber(bar.high)
  const low = finiteNumber(bar.low)
  const close = finiteNumber(bar.close)
  const volume = finiteNumber(bar.volume)
  const timestamp = barTimestamp(bar)
  if (!timestamp || !Number.isFinite(barTimeMs(bar)) || open === null || high === null || low === null || close === null || volume === null) return null
  if (open <= 0 || high <= 0 || low <= 0 || close <= 0 || volume < 0 || low > Math.min(open, close) || high < Math.max(open, close)) return null
  const amount = bar.amount === undefined || bar.amount === null ? undefined : finiteNumber(bar.amount)
  const turnoverRate = bar.turnover_rate === undefined || bar.turnover_rate === null ? null : finiteNumber(bar.turnover_rate)
  return {
    ...bar,
    time: timestamp,
    open,
    high,
    low,
    close,
    volume,
    ...(amount === null ? {} : { amount }),
    turnover_rate: turnoverRate,
  }
}

export function mergeKlineBars(current: KlineBar[], incoming: KlineBar[]) {
  const merged = new Map<number, KlineBar>()
  for (const bar of [...current, ...incoming]) {
    const normalized = validKlineBar(bar)
    if (!normalized) continue
    const timestamp = barTimeMs(normalized)
    if (!Number.isFinite(timestamp)) continue
    merged.set(timestamp, normalized)
  }
  return [...merged.entries()].sort(([left], [right]) => left - right).map(([, bar]) => bar)
}

export function trimBarsToRange(bars: KlineBar[], plan: KlineRangePlan) {
  const ordered = mergeKlineBars([], bars).filter((bar) => {
    const timestamp = barTimeMs(bar)
    return timestamp >= plan.startMs && timestamp <= plan.endMs
  })
  if (!plan.tradingDays) return ordered
  const days = [...new Set(ordered.map((bar) => SHANGHAI_DAY.format(new Date(barTimeMs(bar)))))].slice(-plan.tradingDays)
  const selected = new Set(days)
  return ordered.filter((bar) => selected.has(SHANGHAI_DAY.format(new Date(barTimeMs(bar)))))
}

export function loadedRangeLabel(bars: KlineBar[], period: string) {
  if (!bars.length) return '—'
  const options: Intl.DateTimeFormatOptions = period === 'day'
    ? { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }
    : { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }
  const format = new Intl.DateTimeFormat('zh-CN', options)
  return `${format.format(new Date(barTimeMs(bars[0])))} — ${format.format(new Date(barTimeMs(bars[bars.length - 1])))}`
}

export function klineCoverage(plan: KlineRangePlan, requestedStart: string | undefined, bars: KlineBar[]) {
  if (plan.tradingDays) {
    const days = new Set(bars.map((bar) => SHANGHAI_DAY.format(new Date(barTimeMs(bar)))))
    return { complete: days.size >= plan.tradingDays, progress: Math.min(1, days.size / plan.tradingDays) }
  }
  if (!requestedStart) return { complete: false, progress: 0 }
  const requestedBoundary = Date.parse(requestedStart)
  const firstData = bars.length ? barTimeMs(bars[0]) : Number.POSITIVE_INFINITY
  const boundaryReached = requestedBoundary <= plan.startMs
  // 周末与长假允许目标起点后出现首根有效 K 线，但不能把上游截断数月的数据误报为完整覆盖。
  const complete = boundaryReached && firstData <= plan.startMs + 14 * DAY_MS
  const covered = plan.endMs - Math.max(plan.startMs, firstData)
  return { complete, progress: complete ? 1 : Math.max(0, Math.min(1, covered / Math.max(1, plan.endMs - plan.startMs))) }
}
