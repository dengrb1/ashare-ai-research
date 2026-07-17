import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useMemo, useState, type ReactNode } from 'react'

export type Theme = 'light' | 'dark'
export type ThemeMode = 'system' | Theme

const STORAGE_KEY = 'ashare-theme'
const THEME_COLORS: Record<Theme, string> = { light: '#f4f7f8', dark: '#071018' }

interface ThemeValue {
  theme: ThemeMode
  resolvedTheme: Theme
  setTheme: (theme: ThemeMode) => void
  toggleTheme: () => void
}

const ThemeContext = createContext<ThemeValue | null>(null)

function savedTheme(): ThemeMode {
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    return saved === 'light' || saved === 'dark' || saved === 'system' ? saved : 'system'
  } catch {
    return 'system'
  }
}

function systemTheme(): Theme {
  return window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

function applyTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme
  document.documentElement.style.colorScheme = theme
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', THEME_COLORS[theme])
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, updateTheme] = useState<ThemeMode>(savedTheme)
  const [systemResolvedTheme, setSystemResolvedTheme] = useState<Theme>(systemTheme)
  const resolvedTheme = theme === 'system' ? systemResolvedTheme : theme

  useEffect(() => {
    if (!window.matchMedia) return undefined
    const media = window.matchMedia('(prefers-color-scheme: dark)')
    const update = (event: MediaQueryListEvent | MediaQueryList) => setSystemResolvedTheme(event.matches ? 'dark' : 'light')
    update(media)
    media.addEventListener?.('change', update)
    return () => media.removeEventListener?.('change', update)
  }, [])

  useLayoutEffect(() => applyTheme(resolvedTheme), [resolvedTheme])

  const setTheme = useCallback((next: ThemeMode) => {
    updateTheme(next)
    try {
      localStorage.setItem(STORAGE_KEY, next)
    } catch {
      // Theme still applies for the current session when storage is unavailable.
    }
  }, [])
  const toggleTheme = useCallback(() => {
    const next: Record<ThemeMode, ThemeMode> = { system: 'light', light: 'dark', dark: 'system' }
    setTheme(next[theme])
  }, [setTheme, theme])
  const value = useMemo(() => ({ theme, resolvedTheme, setTheme, toggleTheme }), [theme, resolvedTheme, setTheme, toggleTheme])

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function useTheme() {
  const value = useContext(ThemeContext)
  if (!value) throw new Error('useTheme must be used within ThemeProvider')
  return value
}

export function ThemeToggle({ compact = false }: { compact?: boolean }) {
  const { theme, resolvedTheme, toggleTheme } = useTheme()
  const labels: Record<ThemeMode, string> = { system: '跟随系统', light: '浅色', dark: '深色' }
  const nextLabels: Record<ThemeMode, string> = { system: '浅色', light: '深色', dark: '跟随系统' }
  const actionLabel = `当前：${labels[theme]}；切换${nextLabels[theme]}主题`
  return <button className={`theme-toggle${compact ? ' compact' : ''}`} onClick={toggleTheme} title={actionLabel} aria-label={actionLabel}>
    <span aria-hidden="true">{theme === 'system' ? '◐' : resolvedTheme === 'light' ? '☾' : '☀'}</span>
    {!compact && <span>{labels[theme]}</span>}
  </button>
}
