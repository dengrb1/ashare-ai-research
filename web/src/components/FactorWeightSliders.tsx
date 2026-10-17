import { useMemo, useState } from 'react'

export interface FourFactorWeights {
  fundamental: number
  technical: number
  sentiment: number
  quality: number
}

const KEYS: Array<keyof FourFactorWeights> = ['fundamental', 'technical', 'sentiment', 'quality']

export function normalizeFactorWeights(current: FourFactorWeights, changedKey: keyof FourFactorWeights, targetVal: number, locked: ReadonlySet<keyof FourFactorWeights> = new Set()): FourFactorWeights {
  if (locked.has(changedKey)) return current
  const unlocked = KEYS.filter((key) => key !== changedKey && !locked.has(key))
  if (!unlocked.length) return current
  const lockedTotal = KEYS.filter((key) => locked.has(key)).reduce((sum, key) => sum + current[key], 0)
  const min = 5
  const maxTarget = 100 - lockedTotal - unlocked.length * min
  const target = Math.max(min, Math.min(maxTarget, Math.round(targetVal)))
  const remainder = 100 - lockedTotal - target
  const previousTotal = unlocked.reduce((sum, key) => sum + Math.max(0, current[key]), 0)
  const next: FourFactorWeights = { ...current, [changedKey]: target }
  const distributable = remainder - unlocked.length * min
  let assigned = 0
  unlocked.forEach((key, index) => {
    const value = index === unlocked.length - 1
      ? remainder - assigned
      : min + Math.floor(distributable * Math.max(0, current[key]) / Math.max(1, previousTotal))
    next[key] = value
    assigned += value
  })
  return next
}

export function FactorWeightSliders({ value, onChange, locked = new Set() }: { value?: FourFactorWeights; onChange?: (next: FourFactorWeights) => void; locked?: ReadonlySet<keyof FourFactorWeights> }) {
  const [local, setLocal] = useState<FourFactorWeights>(value || { fundamental: 35, technical: 35, sentiment: 20, quality: 10 })
  const weights = value || local
  const labels = useMemo(() => ({ fundamental: '基本面', technical: '技术面', sentiment: '情绪面', quality: '质量置信度' }), [])
  function update(key: keyof FourFactorWeights, nextValue: number) {
    const next = normalizeFactorWeights(weights, key, nextValue, locked)
    if (!value) setLocal(next)
    onChange?.(next)
  }
  return <div className="factor-sliders" aria-label="因子权重"><div className="factor-sliders-total">总权重 <strong>{Object.values(weights).reduce((sum, item) => sum + item, 0)}%</strong></div>{KEYS.map((key) => <label key={key}><span>{labels[key]} <strong>{weights[key]}%</strong></span><input type="range" min="5" max="85" step="1" value={weights[key]} disabled={locked.has(key)} onChange={(event) => update(key, Number(event.target.value))} /></label>)}</div>
}
