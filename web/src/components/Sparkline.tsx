export function Sparkline({ values, positive = true, height = 54 }: { values: number[]; positive?: boolean; height?: number }) {
  if (values.length < 2) return <div className="sparkline-placeholder" />
  const width = 160
  const min = Math.min(...values)
  const max = Math.max(...values)
  const range = max - min || 1
  const points = values.map((value, index) => `${(index / (values.length - 1)) * width},${height - ((value - min) / range) * (height - 6) - 3}`).join(' ')
  return <svg className={`sparkline ${positive ? 'up' : 'down'}`} viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" aria-hidden="true">
    <polyline points={points} fill="none" vectorEffect="non-scaling-stroke" />
  </svg>
}
