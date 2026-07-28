import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'

export type ToastTone = 'success' | 'info' | 'warning' | 'error'

type Toast = { id: number; message: string; tone: ToastTone }
type ToastContextValue = { notify: (message: string, tone?: ToastTone) => void }

const ToastContext = createContext<ToastContextValue | null>(null)
const AUTO_DISMISS_MS: Partial<Record<ToastTone, number>> = { success: 4_000, info: 5_000 }
const ICON: Record<ToastTone, string> = { success: '✓', info: 'i', warning: '!', error: '!' }

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const nextId = useRef(0)
  const dismiss = useCallback((id: number) => setToasts((current) => current.filter((item) => item.id !== id)), [])
  const notify = useCallback((message: string, tone: ToastTone = 'info') => {
    if (!message) return
    const id = ++nextId.current
    setToasts((current) => [...current, { id, message, tone }])
    const delay = AUTO_DISMISS_MS[tone]
    if (delay) window.setTimeout(() => dismiss(id), delay)
  }, [dismiss])
  const value = useMemo(() => ({ notify }), [notify])

  return <ToastContext.Provider value={value}>{children}<section className="toast-stack" aria-live="polite" aria-label="即时提示">
    {toasts.map((toast) => <article className={`toast toast-${toast.tone}`} key={toast.id} role={toast.tone === 'error' ? 'alert' : 'status'}>
      <span className="toast-icon" aria-hidden="true">{ICON[toast.tone]}</span><p>{toast.message}</p><button type="button" onClick={() => dismiss(toast.id)} aria-label="关闭提示">×</button>
    </article>)}
  </section></ToastContext.Provider>
}

export function useToast() {
  const value = useContext(ToastContext)
  return value || { notify: () => undefined }
}

export function ErrorToast({ message }: { message?: string }) {
  const value = useContext(ToastContext)
  const previous = useRef<string | undefined>(undefined)
  useEffect(() => {
    if (message && message !== previous.current) value?.notify(message, 'error')
    previous.current = message
  }, [message, value])
  return value ? null : message ? <div className="notice error">{message}</div> : null
}
