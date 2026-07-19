import { api } from './api'
import type { Run } from './types'

const PUBLISHED_STATUSES = new Set(['SUCCEEDED', 'FUSED'])

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
