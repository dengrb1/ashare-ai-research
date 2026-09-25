import { Navigate, Outlet, Route, Routes } from 'react-router'
import { AuthProvider, useAuth } from './context/AuthContext'
import { MarketProvider } from './context/MarketContext'
import { AppShell } from './components/AppShell'
import { LoginPage } from './pages/LoginPage'
import { MonitorPage } from './pages/MonitorPage'
import { StrategiesPage } from './pages/StrategiesPage'
import { AICopilotPage } from './pages/AICopilotPage'
import { SettingsPage } from './pages/SettingsPage'
import { ThemeProvider } from './context/ThemeContext'
import { RefreshProvider } from './context/RefreshContext'
import { ToastProvider } from './context/ToastContext'

function ProtectedApp() {
  const { user, loading } = useAuth()
  if (loading) return <div className="boot-screen"><div className="brand-mark">霁</div><span>正在恢复安全会话…</span></div>
  if (!user) return <Navigate to="/login" replace />
  return <MarketProvider><Outlet /></MarketProvider>
}

function AppRoutes() {
  const { user } = useAuth()
  return <Routes>
    <Route path="/login" element={user ? <Navigate to="/" replace /> : <LoginPage />} />
    <Route element={<ProtectedApp />}>
      <Route element={<AppShell />}>
        <Route index element={<MonitorPage />} />
        <Route path="monitor" element={<MonitorPage />} />
        <Route path="strategies" element={<StrategiesPage />} />
        <Route path="copilot" element={<AICopilotPage />} />
        <Route path="settings" element={<SettingsPage />} />
        {['dashboard', 'market', 'assets', 'exit-advice', 'portfolio'].map((path) => <Route key={path} path={path} element={<Navigate to="/monitor" replace />} />)}
        {['research', 'candidates', 'reports', 'backtest', 'runs'].map((path) => <Route key={path} path={path} element={<Navigate to="/strategies" replace />} />)}
        <Route path="ai-chat" element={<Navigate to="/copilot" replace />} />
        {['admin', 'admin/models', 'admin/system-settings', 'admin/jev-model', 'admin/jev-training', 'profile-data', 'search', 'admin/edge-gateway'].map((path) => <Route key={path} path={path} element={<Navigate to="/settings" replace />} />)}
      </Route>
    </Route>
    <Route path="*" element={<Navigate to="/" replace />} />
  </Routes>
}

export function App() {
  return <ThemeProvider><AuthProvider><ToastProvider><RefreshProvider><AppRoutes /></RefreshProvider></ToastProvider></AuthProvider></ThemeProvider>
}
