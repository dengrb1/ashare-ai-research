import type { KlineBar } from './types'

export type OptionalNumber = number | null

export interface MacdPoint {
  dif: OptionalNumber
  dea: OptionalNumber
  histogram: OptionalNumber
}

export interface KdjPoint {
  k: OptionalNumber
  d: OptionalNumber
  j: OptionalNumber
}

export interface MarketIndicators {
  ma5: OptionalNumber[]
  ma10: OptionalNumber[]
  ma20: OptionalNumber[]
  macd: MacdPoint[]
  kdj: KdjPoint[]
}

function movingAverage(values: number[], period: number) {
  let sum = 0
  return values.map((value, index) => {
    sum += value
    if (index >= period) sum -= values[index - period]
    return index >= period - 1 ? sum / period : null
  })
}

function exponentialMovingAverage(values: number[], period: number) {
  const alpha = 2 / (period + 1)
  const result: number[] = []
  values.forEach((value, index) => { result.push(index ? result[index - 1] + alpha * (value - result[index - 1]) : value) })
  return result
}

export function calculateMacd(bars: KlineBar[]): MacdPoint[] {
  const closes = bars.map((bar) => bar.close)
  const fast = exponentialMovingAverage(closes, 12)
  const slow = exponentialMovingAverage(closes, 26)
  const dif = closes.map((_, index) => index >= 25 ? fast[index] - slow[index] : null)
  const dea: OptionalNumber[] = Array(closes.length).fill(null)
  for (let index = 33; index < closes.length; index += 1) {
    if (index === 33) {
      const seed = dif.slice(25, 34) as number[]
      dea[index] = seed.reduce((sum, value) => sum + value, 0) / seed.length
    } else {
      dea[index] = (dea[index - 1] as number) * 0.8 + (dif[index] as number) * 0.2
    }
  }
  return closes.map((_, index) => ({
    dif: dif[index], dea: dea[index],
    histogram: dif[index] === null || dea[index] === null ? null : 2 * (dif[index] - dea[index]),
  }))
}

export function calculateKdj(bars: KlineBar[]): KdjPoint[] {
  let previousK = 50
  let previousD = 50
  return bars.map((bar, index) => {
    if (index < 8) return { k: null, d: null, j: null }
    const window = bars.slice(index - 8, index + 1)
    const highest = Math.max(...window.map((item) => item.high))
    const lowest = Math.min(...window.map((item) => item.low))
    const rsv = highest === lowest ? 50 : ((bar.close - lowest) / (highest - lowest)) * 100
    const k = previousK * (2 / 3) + rsv / 3
    const d = previousD * (2 / 3) + k / 3
    const j = 3 * k - 2 * d
    previousK = k
    previousD = d
    return { k, d, j }
  })
}

export function calculateMarketIndicators(bars: KlineBar[]): MarketIndicators {
  const closes = bars.map((bar) => bar.close)
  return {
    ma5: movingAverage(closes, 5),
    ma10: movingAverage(closes, 10),
    ma20: movingAverage(closes, 20),
    macd: calculateMacd(bars),
    kdj: calculateKdj(bars),
  }
}
