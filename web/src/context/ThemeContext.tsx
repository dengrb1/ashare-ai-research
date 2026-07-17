import { createContext, useCallback, useContext, useLayoutEffect, useMemo, useState, type ReactNode } from 'react'

export type Theme = 'light' | 'dark'

const STORAGE_KEY = 'ashare-theme'
const THEME_COLORS: Record<Theme, string> = { light: '#f4f7f8', dark: '#071018' }

interface ThemeValue {
  theme: Theme
  setTheme: (theme: Theme) => void
  toggleTheme: () => void
}

const ThemeContext = createContext<ThemeValue | null>(null)

function savedTheme(): Theme {
  try {
    return localStorage.getItem(STORAGE_KEY) === 'dark' ? 'dark' : 'light'
  } catch {
    return 'light'
  }
}

function applyTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme
  document.documentElement.style.colorScheme = theme
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', THEME_COLORS[theme])
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, updateTheme] = useState<Theme>(savedTheme)

  useLayoutEffect(() => applyTheme(theme), [theme])

  const setTheme = useCallback((next: Theme) => {
    updateTheme(next)
    try {
      localStorage.setItem(STORAGE_KEY, next)
    } catch {
      // Theme still applies for the current session when storage is unavailable.
    }
  }, [])
  const toggleTheme = useCallback(() => setTheme(theme === 'light' ? 'dark' : 'light'), [setTheme, theme])
  const value = useMemo(() => ({ theme, setTheme, toggleTheme }), [theme, setTheme, toggleTheme])

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function useTheme() {
  const value = useContext(ThemeContext)
  if (!value) throw new Error('useTheme must be used within ThemeProvider')
  return value
}

export function ThemeToggle({ compact = false }: { compact?: boolean }) {
  const { theme, toggleTheme } = useTheme()
  const nextLabel = theme === 'light' ? '切换深色主题' : '切换浅色主题'
  return <button className={`theme-toggle${compact ? ' compact' : ''}`} onClick={toggleTheme} title={nextLabel} aria-label={nextLabel}>
    <span aria-hidden="true">{theme === 'light' ? '☾' : '☀'}</span>
    {!compact && <span>{theme === 'light' ? '深色' : '浅色'}</span>}
  </button>
}
