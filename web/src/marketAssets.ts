import type { PaperPosition } from './types'

export function marketPrefetchSymbols(watchlist: string[], positions: PaperPosition[]) {
  return Array.from(new Set([...watchlist, ...positions.map((row) => row.symbol)])).sort()
}
