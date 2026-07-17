import { useCallback, useEffect, useRef, useState } from 'react'
import type { Run } from '../types'

const ACTIVE = new Set(['PENDING', 'QUEUED', 'RUNNING', 'PROCESSING'])

export function usePollingTask(loader: (id: string) => Promise<Run>, interval = 2500) {
  const [task, setTask] = useState<Run | null>(null)
  const [error, setError] = useState('')
  const timer = useRef<number | undefined>(undefined)

  const stop = useCallback(() => {
    if (timer.current) window.clearTimeout(timer.current)
    timer.current = undefined
  }, [])

  const poll = useCallback(async (id: string) => {
    stop()
    try {
      const next = await loader(id)
      setTask(next)
      setError('')
      if (ACTIVE.has(next.status.toUpperCase())) timer.current = window.setTimeout(() => void poll(id), interval)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '任务状态查询失败')
      timer.current = window.setTimeout(() => void poll(id), interval * 2)
    }
  }, [interval, loader, stop])

  useEffect(() => stop, [stop])
  return { task, error, watch: poll, stop, setTask }
}
