export const DEFAULT_WATCHLIST = ['600519.SH', '000858.SZ', '300750.SZ', '601318.SH']

export const PAPER_HOLDINGS = [
  { symbol: '600519.SH', name: '贵州茅台', quantity: 100, cost: 1428.5, weight: 0.32 },
  { symbol: '300750.SZ', name: '宁德时代', quantity: 500, cost: 241.2, weight: 0.28 },
  { symbol: '601318.SH', name: '中国平安', quantity: 1200, cost: 52.16, weight: 0.22 },
]

export function marketPrefetchSymbols(watchlist: string[]) {
  return Array.from(new Set([...watchlist, ...PAPER_HOLDINGS.map((row) => row.symbol)])).sort()
}
