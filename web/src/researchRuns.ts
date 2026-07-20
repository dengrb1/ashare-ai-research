import { api } from './api'
import type { Run } from './types'

const PUBLISHED_STATUSES = new Set(['SUCCEEDED', 'FUSED'])
const TERMINAL_STATUSES = new Set(['SUCCEEDED', 'FAILED', 'FUSED', 'CANCELLED'])

export function researchRunTimestamp(run: Run): { label: '开始' | '完成'; value?: string } {
  const terminal = TERMINAL_STATUSES.has(run.status.toUpperCase())
  return terminal && run.completed_at
    ? { label: '完成', value: run.completed_at }
    : { label: '开始', value: run.started_at || run.created_at }
}

export async function resolvePublishedResearchRun(tradingDate?: string, runId?: string): Promise<Run | null> {
  if (runId) {
    if (tradingDate) {
      const dated = await api.researchRuns(50, tradingDate, true)
      const selected = dated.find((item) => item.run_id === runId)
      if (selected) return selected.trading_date ? selected : { ...selected, trading_date: tradingDate }
    }
    const explicit = await api.run(runId)
    if (!explicit.trading_date || !PUBLISHED_STATUSES.has(explicit.status.toUpperCase())) return null
    const detailed = await api.researchRuns(50, explicit.trading_date, true)
    return detailed.find((item) => item.run_id === runId) || explicit
  }
  const rows = await api.researchRuns(1, tradingDate, true)
  const selected = rows[0] || null
  return selected && !selected.trading_date && tradingDate
    ? { ...selected, trading_date: tradingDate }
    : selected
}
