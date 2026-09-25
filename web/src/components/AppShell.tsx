import { useEffect, useRef, useState } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router'
import { Activity, BrainCircuit, FlaskConical, LayoutDashboard, RefreshCw, Settings } from 'lucide-react'
import { useAuth } from '../context/AuthContext'
import { MARKET_REFRESH_INTERVAL_OPTIONS, useMarket } from '../context/MarketContext'
import { formatTime } from './Ui'
import { ThemeToggle } from '../context/ThemeContext'
import { useRefreshControl } from '../context/RefreshContext'
import { NotificationBell } from './NotificationBell'
import packageJson from '../../package.json'

const APP_VERSION = `v${packageJson.version}`

type NavItem = { to: string; label: string; icon: typeof LayoutDashboard; end?: boolean }
type NavGroup = { group: string }

const NAV: Array<NavItem | NavGroup> = [
  { to: '/monitor', label: '盘面监控', icon: LayoutDashboard, end: true },
  { to: '/strategies', label: '策略工作台', icon: FlaskConical },
  { to: '/copilot', label: '研究副驾驶', icon: BrainCircuit },
  { group: '控制面' },
  { to: '/settings', label: '设置', icon: Settings },
]

const ADMIN_NAV: NavItem[] = []

const TITLES: Record<string, [string, string]> = {
  '/': ['盘面监控', '实时行情、K 线与确定性告警'],
  '/monitor': ['盘面监控', '实时行情、K 线与确定性告警'],
  '/strategies': ['策略工作台', '选股池、回测与结构化研究结果'],
  '/copilot': ['研究副驾驶', 'Jev 裁决与低置信度 System-2 诊断'],
  '/settings': ['设置', '运行时、模型、Jev 与个人数据'],
}

export function titleForPathname(pathname: string): [string, string] {
  const normalizedPath = pathname === '/' ? '/' : pathname.replace(/\/+$/, '')
  return TITLES[normalizedPath] || TITLES['/']
}

export function AppShell() {
  const { user, logout } = useAuth()
  const { delayed, source, updatedAt, marketRefreshIntervalSeconds, saveMarketRefreshInterval, assetsSaving } = useMarket()
  const location = useLocation()
  const [title, subtitle] = titleForPathname(location.pathname)
  const isAdmin = user?.role?.toLowerCase() === 'admin'
  const { available, busy, refresh } = useRefreshControl()
  const [menuOpen, setMenuOpen] = useState(false)
  const menuButton = useRef<HTMLButtonElement>(null)
  const navigation = useRef<HTMLElement>(null)

  function closeMenu(restoreFocus = true) {
    setMenuOpen(false)
    if (restoreFocus) window.requestAnimationFrame(() => menuButton.current?.focus())
  }

  useEffect(() => {
    if (!menuOpen) return
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') closeMenu()
    }
    window.addEventListener('keydown', onKeyDown)
    window.requestAnimationFrame(() => navigation.current?.querySelector<HTMLAnchorElement>('a')?.focus())
    return () => {
      document.body.style.overflow = previousOverflow
      window.removeEventListener('keydown', onKeyDown)
    }
  }, [menuOpen])

  useEffect(() => {
    if (menuOpen) setMenuOpen(false)
  }, [location.pathname])

  return <div className="app-shell">
    <button className={`nav-overlay ${menuOpen ? 'visible' : ''}`} aria-label="关闭导航菜单" tabIndex={menuOpen ? 0 : -1} onClick={() => closeMenu()} />
    <aside className={`sidebar ${menuOpen ? 'open' : ''}`} aria-label="主导航">
      <div className="brand"><span className="brand-mark">霁</span><div><strong>霁衡智研</strong><small>A-SHARE RESEARCH</small></div></div>
      <nav ref={navigation}>
        {NAV.map((item, index) => 'group' in item ? <div className="nav-group" key={index}>{item.group}</div> :
          <NavLink key={item.to} to={item.to} end={item.end} onClick={() => closeMenu()} className={({ isActive }) => isActive ? 'active' : ''}>
            <span><item.icon size={16} strokeWidth={1.8} /></span>{item.label}
          </NavLink>)}
        {isAdmin && ADMIN_NAV.map((item) =>
          <NavLink key={item.to} to={item.to} end={item.end} onClick={() => closeMenu()} className={({ isActive }) => isActive ? 'active' : ''}>
            <span><item.icon size={16} strokeWidth={1.8} /></span>{item.label}
          </NavLink>)}
      </nav>
      <div className="sidebar-foot">
        <div className={`feed-state ${delayed ? 'delayed' : ''}`}><i />{delayed ? '行情非实时' : `${source} 行情正常`}</div>
        <small>{updatedAt ? `更新 ${formatTime(updatedAt)}` : `按活跃标的 ${marketRefreshIntervalSeconds} 秒刷新`}</small>
      </div>
    </aside>
    <div className="workspace">
      <header className="topbar">
        <button ref={menuButton} className="mobile-menu-button" aria-label="打开导航菜单" aria-expanded={menuOpen} onClick={() => setMenuOpen(true)}>☰</button>
        <div className="topbar-title"><h1>{title}</h1><p>{subtitle}</p></div>
        <div className="top-actions">
          <button className="refresh-button" onClick={() => void refresh()} disabled={!available || busy} title="刷新当前页面">{busy ? <><span className="refresh-spin"><RefreshCw size={14} /></span>刷新中</> : <><RefreshCw size={14} />刷新</>}</button>
          <label className="market-refresh-interval">自动刷新
            <select aria-label="自动刷新间隔" value={marketRefreshIntervalSeconds} disabled={assetsSaving} onChange={(event) => void saveMarketRefreshInterval(Number(event.target.value) as typeof marketRefreshIntervalSeconds).catch(() => undefined)}>
              {MARKET_REFRESH_INTERVAL_OPTIONS.map((seconds) => <option value={seconds} key={seconds}>{seconds} 秒</option>)}
            </select>
          </label>
          <NotificationBell />
          <ThemeToggle compact />
          <div className="user-menu"><span>{user?.username.slice(0, 1).toUpperCase()}</span><div><strong>{user?.username}</strong><small>{isAdmin ? '管理员' : '研究员'}</small></div><button onClick={() => void logout()} title="退出登录">退出</button></div>
        </div>
      </header>
      {delayed && <div className="stale-banner">上游行情暂不可用，当前展示最近成功缓存。冻结研究与回测快照不受影响。</div>}
      <main className="content"><Outlet /></main>
      <footer className="disclaimer"><span>本系统仅用于研究、回测与模拟组合，不构成投资建议，不接入真实交易。</span><span className="app-version" aria-label={`系统版本 ${APP_VERSION}`}>{APP_VERSION}</span></footer>
    </div>
  </div>
}
