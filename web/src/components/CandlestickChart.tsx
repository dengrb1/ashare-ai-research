import { useCallback, useEffect, useId, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'
import { barTimeMs, barTimestamp, mergeKlineBars } from '../marketKlines'
import { calculateMarketIndicators, type OptionalNumber } from '../marketIndicators'
import type { KlineBar } from '../types'
import { formatAmount, formatNumber } from './Ui'

type SecondaryIndicator = 'volume' | 'macd' | 'kdj'

interface CandlestickChartProps {
  bars: KlineBar[]
  period: string
  hasEarlier?: boolean
  loadingEarlier?: boolean
  onLoadEarlier?: () => void | Promise<void>
}

const WIDTH = 1000
const HEIGHT = 560
const LEFT = 62
const RIGHT = 18
const MAIN_TOP = 30
const MAIN_BOTTOM = 330
const SUB_TOP = 375
const SUB_BOTTOM = 505
const AXIS_Y = 538
const MIN_VISIBLE = 20
const DEFAULT_VISIBLE = 80
const CARD_WIDTH = 235
const CARD_HEIGHT = 292
const CARD_GAP = 12

function optional(value: OptionalNumber, digits = 2) {
  return value === null || !Number.isFinite(value) ? '—' : formatNumber(value, digits)
}

function timeLabel(bar: KlineBar, period: string, detailed = false) {
  const timestamp = barTimeMs(bar)
  if (!Number.isFinite(timestamp)) return barTimestamp(bar) || '—'
  const intraday = period !== 'day'
  return new Intl.DateTimeFormat('zh-CN', detailed
    ? { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: intraday ? '2-digit' : undefined, minute: intraday ? '2-digit' : undefined, hour12: false }
    : { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: intraday ? '2-digit' : undefined, minute: intraday ? '2-digit' : undefined, hour12: false }
  ).format(new Date(timestamp))
}

function points(values: OptionalNumber[], indexes: number[], x: (position: number) => number, y: (value: number) => number) {
  return indexes.flatMap((index, position) => {
    const value = values[index]
    return value === null || !Number.isFinite(value) ? [] : [`${x(position)},${y(value)}`]
  }).join(' ')
}

export function CandlestickChart({ bars, period, hasEarlier = false, loadingEarlier = false, onLoadEarlier }: CandlestickChartProps) {
  const [secondary, setSecondary] = useState<SecondaryIndicator>('volume')
  const [visibleCount, setVisibleCount] = useState(DEFAULT_VISIBLE)
  const [offset, setOffset] = useState(0)
  const [hover, setHover] = useState<{ index: number; x: number; y: number } | null>(null)
  const series = useMemo(() => mergeKlineBars([], bars), [bars])
  const clipId = useId().replaceAll(':', '')
  const svgRef = useRef<SVGSVGElement | null>(null)
  const drag = useRef<{ x: number; offset: number; moved: boolean } | null>(null)
  const previousSeries = useRef({ length: series.length, last: series.at(-1) ? barTimestamp(series.at(-1)!) : '' })
  const nearStartRequested = useRef(false)
  const indicators = useMemo(() => calculateMarketIndicators(series), [series])

  useEffect(() => {
    const previous = previousSeries.current
    const added = series.length - previous.length
    const last = series.at(-1) ? barTimestamp(series.at(-1)!) : ''
    // 前插历史分段时 offset 到最新端的距离天然不变；仅在最新端追加新柱时保持旧视口锚点。
    if (added > 0 && offset > 0 && last !== previous.last) {
      setOffset((value) => Math.min(Math.max(0, series.length - MIN_VISIBLE), value + added))
    }
    previousSeries.current = { length: series.length, last }
  }, [offset, series])

  const count = Math.max(1, Math.min(visibleCount, series.length || visibleCount))
  const maxOffset = Math.max(0, series.length - count)
  const safeOffset = Math.min(offset, maxOffset)
  const end = Math.max(0, series.length - safeOffset)
  const start = Math.max(0, end - count)
  const visible = series.slice(start, end)
  const indexes = visible.map((_, index) => start + index)
  const plotWidth = WIDTH - LEFT - RIGHT
  const xStep = plotWidth / Math.max(1, visible.length)
  const x = (position: number) => LEFT + xStep * position + xStep / 2

  const zoom = useCallback((direction: number) => {
    setVisibleCount((current) => Math.max(MIN_VISIBLE, Math.min(Math.max(MIN_VISIBLE, series.length), current + direction * 20)))
  }, [series.length])

  useEffect(() => {
    const svg = svgRef.current
    if (!svg) return
    const handleWheel = (event: WheelEvent) => {
      event.preventDefault()
      event.stopPropagation()
      zoom(event.deltaY > 0 ? 1 : -1)
    }
    svg.addEventListener('wheel', handleWheel, { passive: false })
    return () => svg.removeEventListener('wheel', handleWheel)
  }, [zoom])

  const requestEarlier = useCallback(() => {
    if (!hasEarlier || loadingEarlier || !onLoadEarlier) return
    void onLoadEarlier()
  }, [hasEarlier, loadingEarlier, onLoadEarlier])

  useEffect(() => {
    if (start <= 5 && safeOffset > 0 && hasEarlier && !nearStartRequested.current) {
      nearStartRequested.current = true
      requestEarlier()
    }
    if (start > 8) nearStartRequested.current = false
  }, [hasEarlier, requestEarlier, safeOffset, start])

  if (!visible.length) return <div className="chart-empty">等待 K 线数据</div>

  const priceValues = visible.flatMap((bar, position) => {
    const index = indexes[position]
    return [bar.low, bar.high, indicators.ma5[index], indicators.ma10[index], indicators.ma20[index]].filter((value): value is number => value !== null && Number.isFinite(value))
  })
  const rawPriceMin = Math.min(...priceValues)
  const rawPriceMax = Math.max(...priceValues)
  const pricePadding = Math.max((rawPriceMax - rawPriceMin) * 0.06, rawPriceMax * 0.002, 0.01)
  const priceMin = rawPriceMin - pricePadding
  const priceMax = rawPriceMax + pricePadding
  const priceRange = priceMax - priceMin || 1
  const priceY = (value: number) => MAIN_TOP + ((priceMax - value) / priceRange) * (MAIN_BOTTOM - MAIN_TOP)

  const volumeMax = Math.max(1, ...visible.map((bar) => Number.isFinite(bar.volume) ? bar.volume : 0))
  const macdValues = indexes.flatMap((index) => {
    const point = indicators.macd[index]
    return [point.dif, point.dea, point.histogram].filter((value): value is number => value !== null && Number.isFinite(value))
  })
  const kdjValues = indexes.flatMap((index) => {
    const point = indicators.kdj[index]
    return [point.k, point.d, point.j].filter((value): value is number => value !== null && Number.isFinite(value))
  })
  const secondaryMin = secondary === 'macd' ? Math.min(0, ...macdValues) : secondary === 'kdj' ? Math.min(0, ...kdjValues) : 0
  const secondaryMax = secondary === 'macd' ? Math.max(0, ...macdValues) : secondary === 'kdj' ? Math.max(100, ...kdjValues) : volumeMax
  const secondaryRange = secondaryMax - secondaryMin || 1
  const secondaryY = (value: number) => SUB_TOP + ((secondaryMax - value) / secondaryRange) * (SUB_BOTTOM - SUB_TOP)

  const resolvedActive = hover !== null && hover.index >= start && hover.index < end ? hover.index : end - 1
  const activePosition = resolvedActive - start
  const active = series[resolvedActive]
  const previous = series[resolvedActive - 1]
  const changePercent = previous?.close ? ((active.close / previous.close) - 1) * 100 : null
  function pointerPosition(event: ReactPointerEvent<SVGSVGElement>) {
    const rect = event.currentTarget.getBoundingClientRect()
    return {
      x: ((event.clientX - rect.left) / Math.max(1, rect.width)) * WIDTH,
      y: ((event.clientY - rect.top) / Math.max(1, rect.height)) * HEIGHT,
    }
  }

  function indexAt(value: number) {
    const position = Math.max(0, Math.min(visible.length - 1, Math.floor((value - LEFT) / xStep)))
    return start + position
  }

  function handlePointerDown(event: ReactPointerEvent<SVGSVGElement>) {
    event.currentTarget.setPointerCapture(event.pointerId)
    drag.current = { x: pointerPosition(event).x, offset: safeOffset, moved: false }
    setHover(null)
  }

  function handlePointerMove(event: ReactPointerEvent<SVGSVGElement>) {
    const position = pointerPosition(event)
    if (drag.current) {
      const delta = position.x - drag.current.x
      if (Math.abs(delta) > 3) drag.current.moved = true
      const next = Math.max(0, Math.min(maxOffset, drag.current.offset + Math.round(delta / Math.max(1, xStep))))
      setOffset(next)
      if (next === maxOffset && delta > 0) requestEarlier()
      setHover(null)
      return
    }
    const insidePlot = position.x >= LEFT && position.x <= WIDTH - RIGHT
      && ((position.y >= MAIN_TOP && position.y <= MAIN_BOTTOM) || (position.y >= SUB_TOP && position.y <= SUB_BOTTOM))
    setHover(insidePlot ? { index: indexAt(position.x), ...position } : null)
  }

  function handlePointerUp(event: ReactPointerEvent<SVGSVGElement>) {
    drag.current = null
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
  }

  const tickPositions = [...new Set([0, .2, .4, .6, .8, 1].map((ratio) => Math.min(visible.length - 1, Math.round((visible.length - 1) * ratio))))]
  const changeAmount = previous ? active.close - previous.close : null
  const amplitude = previous?.close ? ((active.high - active.low) / previous.close) * 100 : null
  const detailItems: Array<[string, string]> = [
    ['时间', timeLabel(active, period, true)],
    ['开盘', formatNumber(active.open)],
    ['收盘', formatNumber(active.close)],
    ['最高', formatNumber(active.high)],
    ['最低', formatNumber(active.low)],
    ['涨跌幅', changePercent === null ? '—' : `${changePercent >= 0 ? '+' : ''}${formatNumber(changePercent)}%`],
    ['涨跌额', changeAmount === null ? '—' : `${changeAmount >= 0 ? '+' : ''}${formatNumber(changeAmount)}`],
    ['成交量', formatAmount(active.volume)],
    ['成交额', active.amount === undefined || active.amount === null ? '—' : formatAmount(active.amount)],
    ['振幅', amplitude === null ? '—' : `${formatNumber(amplitude)}%`],
    ['换手率', active.turnover_rate === undefined || active.turnover_rate === null ? '—' : `${formatNumber(active.turnover_rate)}%`],
  ]
  const cardX = hover ? Math.max(LEFT, Math.min(WIDTH - RIGHT - CARD_WIDTH, hover.x + CARD_GAP + CARD_WIDTH > WIDTH - RIGHT ? hover.x - CARD_GAP - CARD_WIDTH : hover.x + CARD_GAP)) : LEFT
  const cardY = hover ? Math.max(MAIN_TOP, Math.min(SUB_BOTTOM - CARD_HEIGHT, hover.y - CARD_HEIGHT / 2)) : MAIN_TOP

  return <div className="advanced-chart">
    <div className="chart-toolbar">
      <div className="indicator-tabs" aria-label="副图指标">
        {([['volume', '成交量'], ['macd', 'MACD'], ['kdj', 'KDJ']] as Array<[SecondaryIndicator, string]>).map(([value, label]) => <button key={value} className={secondary === value ? 'active' : ''} onClick={() => setSecondary(value)}>{label}</button>)}
      </div>
      <div className="chart-actions">
        <button onClick={() => zoom(-1)} aria-label="放大图表" disabled={count <= MIN_VISIBLE}>＋</button>
        <button onClick={() => zoom(1)} aria-label="缩小图表" disabled={count >= series.length}>－</button>
        <button onClick={() => { setOffset(0); setHover(null) }} disabled={safeOffset === 0}>回到最新</button>
        <button onClick={requestEarlier} disabled={!hasEarlier || loadingEarlier}>{loadingEarlier ? '加载中…' : '加载更早'}</button>
      </div>
    </div>
    <div className="chart-legends">
      <span className="ma5">MA5 {optional(indicators.ma5[resolvedActive])}</span>
      <span className="ma10">MA10 {optional(indicators.ma10[resolvedActive])}</span>
      <span className="ma20">MA20 {optional(indicators.ma20[resolvedActive])}</span>
      <span>{secondary === 'volume' ? '成交量（涨跌着色）' : secondary === 'macd' ? 'MACD 12/26/9' : 'KDJ 9/3/3 · 初始 K/D=50'}</span>
    </div>
    <svg ref={svgRef} className="candle-chart" viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label="价格、均线和技术指标图" onPointerDown={handlePointerDown} onPointerMove={handlePointerMove} onPointerUp={handlePointerUp} onPointerCancel={() => { drag.current = null; setHover(null) }} onPointerLeave={() => { if (!drag.current) setHover(null) }}>
      <defs>
        <clipPath id={clipId + '-price'}><rect x={LEFT} y={MAIN_TOP} width={plotWidth} height={MAIN_BOTTOM - MAIN_TOP} /></clipPath>
        <clipPath id={clipId + '-secondary'}><rect x={LEFT} y={SUB_TOP} width={plotWidth} height={SUB_BOTTOM - SUB_TOP} /></clipPath>
      </defs>
      {[0, 1, 2, 3, 4].map((line) => {
        const y = MAIN_TOP + line * ((MAIN_BOTTOM - MAIN_TOP) / 4)
        const value = priceMax - line * (priceRange / 4)
        return <g key={`price-${line}`}><line x1={LEFT} y1={y} x2={WIDTH - RIGHT} y2={y} className="chart-grid" /><text x={4} y={y + 4} className="chart-label">{formatNumber(value)}</text></g>
      })}
      <line x1={LEFT} y1={SUB_TOP} x2={WIDTH - RIGHT} y2={SUB_TOP} className="chart-grid" />
      <line x1={LEFT} y1={SUB_BOTTOM} x2={WIDTH - RIGHT} y2={SUB_BOTTOM} className="chart-grid" />
      <text x={4} y={SUB_TOP + 4} className="chart-label">{secondary === 'volume' ? formatAmount(secondaryMax) : formatNumber(secondaryMax)}</text>
      <text x={4} y={SUB_BOTTOM + 4} className="chart-label">{secondary === 'volume' ? '0' : formatNumber(secondaryMin)}</text>

      <g clipPath={'url(#' + clipId + '-price)'}>
      <polyline points={points(indicators.ma5, indexes, x, priceY)} className="indicator-line ma5-line" fill="none" />
      <polyline points={points(indicators.ma10, indexes, x, priceY)} className="indicator-line ma10-line" fill="none" />
      <polyline points={points(indicators.ma20, indexes, x, priceY)} className="indicator-line ma20-line" fill="none" />
      {visible.map((bar, position) => {
        const candleX = x(position)
        const up = bar.close >= bar.open
        const bodyTop = priceY(Math.max(bar.open, bar.close))
        const bodyHeight = Math.max(1.5, Math.abs(priceY(bar.open) - priceY(bar.close)))
        return <g key={`${barTimestamp(bar)}-${position}`} className={up ? 'candle-up' : 'candle-down'}>
          <line x1={candleX} x2={candleX} y1={priceY(bar.high)} y2={priceY(bar.low)} />
          <rect x={candleX - Math.max(1.2, xStep * .28)} y={bodyTop} width={Math.max(2.4, xStep * .56)} height={bodyHeight} />
        </g>
      })}
      </g>

      <g clipPath={'url(#' + clipId + '-secondary)'}>
      {secondary === 'volume' && visible.map((bar, position) => {
        if (!Number.isFinite(bar.volume)) return null
        const up = bar.close >= bar.open
        const top = secondaryY(bar.volume)
        return <rect key={`volume-${barTimestamp(bar)}-${position}`} className={up ? 'volume-up' : 'volume-down'} x={x(position) - Math.max(1, xStep * .32)} y={top} width={Math.max(2, xStep * .64)} height={Math.max(1, SUB_BOTTOM - top)} />
      })}
      {secondary === 'macd' && <>
        <line x1={LEFT} y1={secondaryY(0)} x2={WIDTH - RIGHT} y2={secondaryY(0)} className="chart-zero" />
        {indexes.map((index, position) => {
          const value = indicators.macd[index].histogram
          if (value === null) return null
          const zero = secondaryY(0)
          const valueY = secondaryY(value)
          return <rect key={`macd-${index}`} className={value >= 0 ? 'volume-up' : 'volume-down'} x={x(position) - Math.max(1, xStep * .28)} y={Math.min(zero, valueY)} width={Math.max(2, xStep * .56)} height={Math.max(1, Math.abs(zero - valueY))} />
        })}
        <polyline points={points(indicators.macd.map((point) => point.dif), indexes, x, secondaryY)} className="indicator-line dif-line" fill="none" />
        <polyline points={points(indicators.macd.map((point) => point.dea), indexes, x, secondaryY)} className="indicator-line dea-line" fill="none" />
      </>}
      {secondary === 'kdj' && <>
        <polyline points={points(indicators.kdj.map((point) => point.k), indexes, x, secondaryY)} className="indicator-line k-line" fill="none" />
        <polyline points={points(indicators.kdj.map((point) => point.d), indexes, x, secondaryY)} className="indicator-line d-line" fill="none" />
        <polyline points={points(indicators.kdj.map((point) => point.j), indexes, x, secondaryY)} className="indicator-line j-line" fill="none" />
      </>}
      </g>

      {tickPositions.map((position) => <g key={`time-${position}`}><line x1={x(position)} y1={SUB_BOTTOM} x2={x(position)} y2={SUB_BOTTOM + 5} className="chart-grid" /><text x={x(position)} y={AXIS_Y} textAnchor="middle" className="chart-label">{timeLabel(visible[position], period)}</text></g>)}
      {hover && <>
        <line x1={x(activePosition)} y1={MAIN_TOP} x2={x(activePosition)} y2={SUB_BOTTOM} className="chart-crosshair" />
        <line x1={LEFT} y1={priceY(active.close)} x2={WIDTH - RIGHT} y2={priceY(active.close)} className="chart-crosshair" />
        <circle cx={x(activePosition)} cy={priceY(active.close)} r="3" className="chart-focus" />
        <foreignObject x={cardX} y={cardY} width={CARD_WIDTH} height={CARD_HEIGHT} className="chart-hover-card" aria-live="polite">
          <div className={changeAmount !== null && changeAmount < 0 ? 'negative' : 'positive'}>{detailItems.map(([label, value]) => <div key={label}><span>{label}</span><strong>{value}</strong></div>)}</div>
        </foreignObject>
      </>}
    </svg>
  </div>
}
