import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'

type RefreshHandler = () => void | Promise<void>

interface RefreshValue {
  busy: boolean
  available: boolean
  refresh: () => Promise<void>
  register: (handler: RefreshHandler | null) => () => void
}

const RefreshContext = createContext<RefreshValue | null>(null)

export function RefreshProvider({ children }: { children: ReactNode }) {
  const handler = useRef<RefreshHandler | null>(null)
  const [busy, setBusy] = useState(false)
  const [available, setAvailable] = useState(false)

  const register = useCallback((next: RefreshHandler | null) => {
    handler.current = next
    setAvailable(Boolean(next))
    return () => {
      if (handler.current === next) {
        handler.current = null
        setAvailable(false)
      }
    }
  }, [])

  const refresh = useCallback(async () => {
    if (busy || !handler.current) return
    setBusy(true)
    try { await handler.current() } finally { setBusy(false) }
  }, [busy])

  const value = useMemo(() => ({ busy, available, refresh, register }), [available, busy, refresh, register])
  return <RefreshContext.Provider value={value}>{children}</RefreshContext.Provider>
}

export function useRefreshControl() {
  const value = useContext(RefreshContext)
  if (!value) throw new Error('useRefreshControl must be used within RefreshProvider')
  return value
}

export function usePageRefresh(handler: RefreshHandler) {
  const { register } = useRefreshControl()
  useEffect(() => register(handler), [handler, register])
}
