import type { PaperPosition } from './types'

export function marketPrefetchSymbols(watchlist: string[], positions: PaperPosition[]) {
  return Array.from(new Set([...watchlist, ...positions.map((row) => row.symbol)])).sort()
}

export function validateAShareOrderInput(symbol: string, quantity: number, cost: number): { valid: boolean; errors: string[] } {
  const code = symbol.trim().toUpperCase().split('.', 1)[0]
  const isStar = code.startsWith('688')
  const isBeijing = symbol.trim().toUpperCase().endsWith('.BJ') || /^(43|83|87|920)/.test(code)
  const minimum = isStar ? 200 : 100
  const step = isStar || isBeijing ? 1 : 100
  const errors: string[] = []
  if (!/^\d{6}$/.test(code)) errors.push('证券代码必须是 6 位数字')
  if (!Number.isInteger(quantity) || quantity < minimum || quantity % step !== 0) errors.push(`${isStar ? '科创板' : isBeijing ? '北交所' : '沪深'}数量须为 ${minimum} 股起、${step} 股递增`)
  if (!Number.isFinite(cost) || cost <= 0 || Math.abs(cost * 100 - Math.round(cost * 100)) > 1e-7) errors.push('价格必须为正数且按 0.01 元递增')
  return { valid: errors.length === 0, errors }
}
