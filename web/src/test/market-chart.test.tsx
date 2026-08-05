import { fireEvent, render, screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { CandlestickChart } from '../components/CandlestickChart'
import { calculateKdj, calculateMacd, calculateMarketIndicators } from '../marketIndicators'
import { getKlineChunkWindow, getKlineRangePlan, klineCoverage, mergeKlineBars, trimBarsToRange } from '../marketKlines'
import type { KlineBar } from '../types'

function bars(count: number, start = '2026-01-01T01:30:00+08:00'): KlineBar[] {
  const startMs = Date.parse(start)
  return Array.from({ length: count }, (_, index) => ({
    time: new Date(startMs + index * 60_000).toISOString(),
    open: index + 1,
    high: index + 2,
    low: Math.max(0.1, index),
    close: index + 1.5,
    volume: 100 + index,
    amount: index === count - 1 ? undefined : 10_000 + index,
    turnover_rate: index === count - 1 ? null : 1.25,
  }))
}

describe('market range and chunk planning', () => {
  it('calculates rolling Shanghai ranges across month and year boundaries', () => {
    const monthEnd = getKlineRangePlan('1m', new Date('2026-03-31T02:00:00Z'))
    expect(monthEnd.end).toBe('2026-03-31T10:00:00+08:00')
    expect(monthEnd.start).toBe('2026-02-28T10:00:00+08:00')

    const year = getKlineRangePlan('1y', new Date('2026-01-02T08:00:00Z'))
    expect(year.start).toBe('2025-01-02T16:00:00+08:00')
    const rollingNow = new Date('2026-07-17T04:00:00Z')
    expect(getKlineRangePlan('1m', rollingNow).start).toBe('2026-06-17T12:00:00+08:00')
    expect(getKlineRangePlan('3m', rollingNow).start).toBe('2026-04-17T12:00:00+08:00')
    expect(getKlineRangePlan('6m', rollingNow).start).toBe('2026-01-17T12:00:00+08:00')
    expect(getKlineRangePlan('1y', rollingNow).start).toBe('2025-07-17T12:00:00+08:00')
    expect(getKlineRangePlan('1d', rollingNow).tradingDays).toBe(1)
    expect(getKlineRangePlan('5d', new Date('2026-01-05T00:00:00Z')).tradingDays).toBe(5)
  })

  it('uses period-specific chunks and never crosses the selected target boundary', () => {
    const plan = getKlineRangePlan('1y', new Date('2026-07-17T04:00:00Z'))
    const expectedDays = { '1m': 7, '5m': 30, '15m': 90, '30m': 180, '60m': 365 }
    Object.entries(expectedDays).forEach(([period, days]) => {
      const chunk = getKlineChunkWindow(period, plan)!
      expect(Date.parse(chunk.end) - Date.parse(chunk.start)).toBe(days * 24 * 60 * 60 * 1000)
    })
    const latest = getKlineChunkWindow('1m', plan)!
    const earlier = getKlineChunkWindow('1m', plan, latest.start)!
    expect(latest.key).toBe('latest')
    expect(earlier.key).not.toBe('latest')
    expect(Date.parse(earlier.end)).toBe(Date.parse(latest.start))
    expect(Date.parse(earlier.start)).toBeGreaterThanOrEqual(plan.startMs)
    expect(getKlineChunkWindow('day', plan)?.start).toBe(new Date(plan.startMs).toISOString())
  })

  it('merges unordered chunks by timestamp, replaces duplicates, and selects actual data days', () => {
    const first = bars(3)
    const replacement = { ...first[1], high: 99, close: 99 }
    const merged = mergeKlineBars([first[2], first[0]], [replacement])
    expect(merged.map((bar) => bar.close)).toEqual([1.5, 99, 3.5])

    const daily: KlineBar[] = ['2026-07-10', '2026-07-13', '2026-07-14', '2026-07-15', '2026-07-16', '2026-07-17'].map((time, index) => ({ time, open: 1, high: 2 + index, low: .5, close: 1 + index, volume: 1 }))
    const plan = getKlineRangePlan('5d', new Date('2026-07-19T04:00:00Z'))
    const selected = trimBarsToRange(daily, plan)
    expect(selected.map((bar) => bar.time)).toEqual(['2026-07-13', '2026-07-14', '2026-07-15', '2026-07-16', '2026-07-17'])
    expect(klineCoverage(plan, plan.start, selected)).toEqual({ complete: true, progress: 1 })
    const oneDay = getKlineRangePlan('1d', new Date('2026-07-19T04:00:00Z'))
    expect(trimBarsToRange(daily, oneDay).map((bar) => bar.time)).toEqual(['2026-07-17'])

    const annual = getKlineRangePlan('1y', new Date('2026-07-17T04:00:00Z'))
    const truncated = daily.map((bar) => ({ ...bar, time: bar.time?.replace('2026-07', '2026-06') }))
    expect(klineCoverage(annual, annual.start, truncated).complete).toBe(false)
  })

  it('drops malformed OHLC rows before they can distort the chart scale', () => {
    const valid = bars(2)
    const malformed = { ...valid[1], low: 0 } as KlineBar
    const merged = mergeKlineBars([], [valid[0], malformed])
    expect(merged).toHaveLength(1)
    expect(merged[0].time).toBe(valid[0].time)
  })
})

describe('market indicators and chart interaction', () => {
  it('calculates MA, MACD and KDJ with insufficient samples left blank', () => {
    const rows = bars(40)
    const result = calculateMarketIndicators(rows)
    expect(result.ma5.slice(0, 4)).toEqual([null, null, null, null])
    expect(result.ma5[4]).toBeCloseTo(3.5)
    expect(result.ma20[19]).toBeCloseTo(11)
    expect(calculateMacd(rows)[24]).toEqual({ dif: null, dea: null, histogram: null })
    expect(result.macd[39].dif).toBeCloseTo(6.3867273176)
    expect(result.macd[39].dea).toBeCloseTo(6.1285950278)
    expect(result.macd[39].histogram).toBeCloseTo(.5162645797)
  })

  it('keeps flat KDJ at the neutral initial value instead of treating zero range as zero', () => {
    const flat = Array.from({ length: 12 }, (_, index) => ({ time: `2026-01-${String(index + 1).padStart(2, '0')}`, open: 10, high: 10, low: 10, close: 10, volume: 0 }))
    const kdj = calculateKdj(flat)
    expect(kdj[7]).toEqual({ k: null, d: null, j: null })
    expect(kdj[8]).toEqual({ k: 50, d: 50, j: 50 })
  })

  it('shows the complete hover card, removes fixed details, and hides on leave', async () => {
    const onLoadEarlier = vi.fn()
    const { container } = render(<CandlestickChart bars={bars(90)} period="1m" hasEarlier onLoadEarlier={onLoadEarlier} />)
    const svg = screen.getByRole('img')
    vi.spyOn(svg, 'getBoundingClientRect').mockReturnValue({ x: 0, y: 0, left: 0, top: 0, right: 1000, bottom: 560, width: 1000, height: 560, toJSON: () => ({}) })
    expect(container.querySelector('.chart-detail')).not.toBeInTheDocument()
    fireEvent.pointerMove(svg, { clientX: 981, clientY: 180 })
    const card = container.querySelector('.chart-hover-card')!
    expect(card).toBeInTheDocument()
    for (const label of ['时间', '开盘', '收盘', '最高', '最低', '涨跌幅', '涨跌额', '成交量', '成交额', '振幅', '换手率']) {
      expect(card.textContent).toContain(label)
    }
    expect(Number(card.getAttribute('x'))).toBeLessThan(981)
    expect(Array.from(card.querySelectorAll('strong')).filter((item) => item.textContent === '—')).toHaveLength(2)
    fireEvent.pointerLeave(svg)
    expect(container.querySelector('.chart-hover-card')).not.toBeInTheDocument()
  })

  it('switches secondary panels, cancels native wheel scrolling, zooms, and requests earlier data', () => {
    const onLoadEarlier = vi.fn()
    const { container } = render(<CandlestickChart bars={bars(90)} period="1m" hasEarlier onLoadEarlier={onLoadEarlier} />)
    const chart = within(container)
    fireEvent.click(chart.getByRole('button', { name: 'MACD' }))
    expect(chart.getByText(/MACD 12\/26\/9/)).toBeInTheDocument()
    fireEvent.click(chart.getByRole('button', { name: 'KDJ' }))
    expect(chart.getByText(/KDJ 9\/3\/3/)).toBeInTheDocument()
    const svg = chart.getByRole('img')
    const parentWheel = vi.fn()
    container.addEventListener('wheel', parentWheel)
    const before = container.querySelectorAll('.candle-up, .candle-down').length
    const event = new WheelEvent('wheel', { deltaY: -100, bubbles: true, cancelable: true })
    fireEvent(svg, event)
    expect(event.defaultPrevented).toBe(true)
    expect(parentWheel).not.toHaveBeenCalled()
    expect(container.querySelectorAll('.candle-up, .candle-down').length).toBeLessThan(before)
    expect(chart.getByRole('button', { name: '缩小图表' })).not.toBeDisabled()
    fireEvent.click(chart.getByRole('button', { name: '加载更早' }))
    expect(onLoadEarlier).toHaveBeenCalledTimes(1)
  })
})
