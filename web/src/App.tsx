import { Navigate, Outlet, Route, Routes } from 'react-router-dom'
import { AuthProvider, useAuth } from './context/AuthContext'
import { MarketProvider } from './context/MarketContext'
import { AppShell } from './components/AppShell'
import { LoginPage } from './pages/LoginPage'
import { DashboardPage } from './pages/DashboardPage'
import { MarketPage } from './pages/MarketPage'
import { AssetsPage } from './pages/AssetsPage'
import { ResearchPage } from './pages/ResearchPage'
import { CandidatesPage } from './pages/CandidatesPage'
import { PortfolioPage } from './pages/PortfolioPage'
import { ReportsPage } from './pages/ReportsPage'
import { RunsPage } from './pages/RunsPage'
import { BacktestPage } from './pages/BacktestPage'
import { AdminPage } from './pages/AdminPage'
import { FinancialSearchPage } from './pages/FinancialSearchPage'
import { ModelSettingsPage } from './pages/ModelSettingsPage'
import { ThemeProvider } from './context/ThemeContext'

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
        <Route index element={<DashboardPage />} />
        <Route path="market" element={<MarketPage />} />
        <Route path="search" element={<FinancialSearchPage />} />
        <Route path="assets" element={<AssetsPage />} />
        <Route path="research" element={<ResearchPage />} />
        <Route path="candidates" element={<CandidatesPage />} />
        <Route path="portfolio" element={<PortfolioPage />} />
        <Route path="reports" element={<ReportsPage />} />
        <Route path="runs" element={<RunsPage />} />
        <Route path="backtest" element={<BacktestPage />} />
        <Route path="admin" element={<AdminPage />} />
        <Route path="admin/models" element={<ModelSettingsPage />} />
      </Route>
    </Route>
    <Route path="*" element={<Navigate to="/" replace />} />
  </Routes>
}

export function App() {
  return <ThemeProvider><AuthProvider><AppRoutes /></AuthProvider></ThemeProvider>
}
