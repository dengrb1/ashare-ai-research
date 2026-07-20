import { afterEach, describe, expect, it, vi } from 'vitest'
import { today } from '../components/Ui'
import { researchRunTimestamp } from '../researchRuns'

afterEach(() => {
  vi.useRealTimers()
})

describe('Shanghai calendar helpers', () => {
  it('uses the Shanghai date even when UTC is still on the previous day', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-07-19T16:30:00.000Z'))
    expect(today()).toBe('2026-07-20')
  })

  it('uses completion time for terminal research runs and start time for active runs', () => {
    expect(researchRunTimestamp({
      run_id: 'failed-run', status: 'FAILED',
      started_at: '2026-07-20T07:09:45Z', completed_at: '2026-07-20T07:30:09Z',
    })).toEqual({ label: '完成', value: '2026-07-20T07:30:09Z' })
    expect(researchRunTimestamp({
      run_id: 'active-run', status: 'RUNNING', started_at: '2026-07-20T07:09:45Z',
    })).toEqual({ label: '开始', value: '2026-07-20T07:09:45Z' })
  })
})
