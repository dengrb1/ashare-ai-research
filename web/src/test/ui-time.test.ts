import { afterEach, describe, expect, it, vi } from 'vitest'
import { today } from '../components/Ui'

afterEach(() => {
  vi.useRealTimers()
})

describe('Shanghai calendar helpers', () => {
  it('uses the Shanghai date even when UTC is still on the previous day', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-07-19T16:30:00.000Z'))
    expect(today()).toBe('2026-07-20')
  })
})
