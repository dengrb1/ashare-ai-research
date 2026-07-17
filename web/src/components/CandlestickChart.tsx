import type { KlineBar } from '../types'

export function CandlestickChart({ bars }: { bars: KlineBar[] }) {
  const visible = bars.slice(-80)
  if (!visible.length) return <div className="chart-empty">等待 K 线数据</div>
  const width = 960
  const height = 360
  const pad = 26
  const min = Math.min(...visible.map((bar) => bar.low))
  const max = Math.max(...visible.map((bar) => bar.high))
  const range = max - min || 1
  const xStep = (width - pad * 2) / visible.length
  const y = (value: number) => pad + ((max - value) / range) * (height - pad * 2)
  const avg = visible.map((_, index) => {
    const slice = visible.slice(Math.max(0, index - 4), index + 1)
    return slice.reduce((sum, row) => sum + row.close, 0) / slice.length
  })
  const avgPoints = avg.map((value, index) => `${pad + index * xStep + xStep / 2},${y(value)}`).join(' ')
  return <svg className="candle-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="后复权 K 线图">
    {[0, 1, 2, 3, 4].map((line) => <line key={line} x1={pad} y1={pad + line * ((height - pad * 2) / 4)} x2={width - pad} y2={pad + line * ((height - pad * 2) / 4)} className="chart-grid" />)}
    <polyline points={avgPoints} className="ma-line" fill="none" />
    {visible.map((bar, index) => {
      const x = pad + index * xStep + xStep / 2
      const up = bar.close >= bar.open
      const bodyTop = y(Math.max(bar.open, bar.close))
      const bodyHeight = Math.max(1.5, Math.abs(y(bar.open) - y(bar.close)))
      return <g key={`${bar.time || bar.trading_date || index}-${index}`} className={up ? 'candle-up' : 'candle-down'}>
        <line x1={x} x2={x} y1={y(bar.high)} y2={y(bar.low)} />
        <rect x={x - Math.max(1.2, xStep * .27)} y={bodyTop} width={Math.max(2.4, xStep * .54)} height={bodyHeight} />
      </g>
    })}
    <text x={pad} y={16} className="chart-label">{max.toFixed(2)}</text>
    <text x={pad} y={height - 6} className="chart-label">{min.toFixed(2)}</text>
  </svg>
}
