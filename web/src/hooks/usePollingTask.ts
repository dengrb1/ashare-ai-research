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

export type TaskMachineState = 'IDLE' | 'SUBMITTED' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'CANCELLED'

export interface TaskErrorModalPayload {
  title: string
  detail: string
  code?: string
}

export function useAsyncTaskMachine<T>({
  pollFn,
  cancelFn,
  resolveState,
  resolveProgress,
  intervalMs = 2000,
  maxErrors = 3,
  timeoutMs = 180_000,
}: {
  pollFn: (id: string, signal: AbortSignal) => Promise<T>
  cancelFn?: (id: string) => Promise<unknown>
  resolveState: (payload: T) => TaskMachineState
  resolveProgress?: (payload: T) => { percent: number; label: string }
  intervalMs?: number
  maxErrors?: number
  timeoutMs?: number
}) {
  const [taskId, setTaskId] = useState<string | null>(null)
  const [state, setState] = useState<TaskMachineState>('IDLE')
  const [progress, setProgress] = useState({ percent: 0, label: '待命' })
  const [result, setResult] = useState<T | null>(null)
  const [errorModal, setErrorModal] = useState<TaskErrorModalPayload | null>(null)
  const timerRef = useRef<number | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const errorsRef = useRef(0)
  const startedAtRef = useRef(0)
  const lastSubmitterRef = useRef<(() => Promise<string>) | null>(null)

  const clearPending = useCallback(() => {
    if (timerRef.current !== null) window.clearTimeout(timerRef.current)
    timerRef.current = null
    abortRef.current?.abort()
    abortRef.current = null
  }, [])

  const poll = useCallback(async (id: string) => {
    if (Date.now() - startedAtRef.current > timeoutMs) {
      clearPending(); setState('FAILED')
      setErrorModal({ title: '任务执行超时', detail: `任务 ${id} 超出最大等待时间，已自动中断保护。`, code: 'TIMEOUT' })
      return
    }
    const controller = new AbortController()
    abortRef.current = controller
    try {
      const next = await pollFn(id, controller.signal)
      errorsRef.current = 0
      setResult(next)
      if (resolveProgress) setProgress(resolveProgress(next))
      const mapped = resolveState(next)
      if (mapped === 'SUCCEEDED' || mapped === 'FAILED' || mapped === 'CANCELLED') {
        clearPending(); setState(mapped)
        if (mapped === 'FAILED') setErrorModal({ title: '任务执行失败', detail: '服务端返回失败状态，请核对参数后重试。', code: 'JOB_FAILED' })
        return
      }
      setState('RUNNING')
      timerRef.current = window.setTimeout(() => void poll(id), intervalMs)
    } catch (reason) {
      if ((reason as Error)?.name === 'AbortError') return
      errorsRef.current += 1
      if (errorsRef.current >= maxErrors) {
        clearPending(); setState('FAILED')
        setErrorModal({ title: '任务状态同步中断', detail: reason instanceof Error ? reason.message : '连续多次轮询失败，已熔断防假死。', code: 'POLL_CIRCUIT_BROKEN' })
        return
      }
      timerRef.current = window.setTimeout(() => void poll(id), intervalMs * 1.5)
    }
  }, [clearPending, intervalMs, maxErrors, pollFn, resolveProgress, resolveState, timeoutMs])

  const start = useCallback(async (submitter: () => Promise<string>) => {
    clearPending(); lastSubmitterRef.current = submitter; errorsRef.current = 0; startedAtRef.current = Date.now()
    setErrorModal(null); setState('SUBMITTED'); setProgress({ percent: 10, label: '已提交队列，等待调度…' })
    try {
      const id = await submitter()
      setTaskId(id); setState('RUNNING'); setProgress({ percent: 25, label: '正在计算因子与执行策略…' }); void poll(id)
    } catch (reason) {
      setState('FAILED'); setErrorModal({ title: '提交任务失败', detail: reason instanceof Error ? reason.message : '请求被服务端拒绝', code: 'SUBMIT_ERROR' })
    }
  }, [clearPending, poll])

  const cancel = useCallback(async () => {
    clearPending()
    if (taskId && cancelFn) { try { await cancelFn(taskId) } catch { /* local state still releases the lock */ } }
    setState('CANCELLED'); setProgress({ percent: 0, label: '已取消' })
  }, [cancelFn, clearPending, taskId])

  const retry = useCallback(() => { if (lastSubmitterRef.current) void start(lastSubmitterRef.current) }, [start])
  useEffect(() => clearPending, [clearPending])
  return { taskId, state, progress, result, errorModal, start, cancel, retry, closeErrorModal: () => setErrorModal(null) }
}
