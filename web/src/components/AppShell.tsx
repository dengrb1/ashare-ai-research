import { useEffect, useRef, useState } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { useMarket } from '../context/MarketContext'
import { formatTime } from './Ui'
import { ThemeToggle } from '../context/ThemeContext'
import { useRefreshControl } from '../context/RefreshContext'
import { NotificationBell } from './NotificationBell'

const NAV = [
  { to: '/', label: '全局仪表盘', icon: '◫', end: true },
  { to: '/market', label: '行情与 K 线', icon: '⌁' },
  { to: '/search', label: '金融数据搜索', icon: '⌕' },
  { to: '/assets', label: '自选与持仓', icon: '◇' },
  { to: '/profile-data', label: '个人档案', icon: '⇳' },
  { group: '研究中心' },
  { to: '/research', label: '每日研究', icon: '✦' },
  { to: '/exit-advice', label: '卖出建议', icon: '⇲' },
  { to: '/ai-chat', label: 'AI 股票问答', icon: '◌' },
  { to: '/candidates', label: '候选池', icon: '◎' },
  { to: '/portfolio', label: '模拟组合', icon: '▥' },
  { to: '/reports', label: '研究报告', icon: '▤' },
  { group: '任务与系统' },
  { to: '/backtest', label: '回测工作台', icon: '⟲' },
  { to: '/runs', label: '运行与审计', icon: '≡' },
  { group: '管理' },
  { to: '/about', label: '关于本系统', icon: 'ⓘ' },
]

const TITLES: Record<string, [string, string]> = {
  '/': ['全局仪表盘', '研究运行、市场状态与组合概览'],
  '/market': ['行情与 K 线', '活跃标的按需刷新 · 历史序列后复权'],
  '/search': ['金融数据搜索', 'AI 解析意图 · 确定性数据源返回金融事实'],
  '/assets': ['自选与持仓', '关注列表与个人持仓记录'],
  '/profile-data': ['个人档案', '加密导出 · 安全预览 · 分类合并导入'],
  '/research': ['每日研究', '基于冻结快照的可复现异步研究'],
  '/exit-advice': ['卖出建议', '盘中盈利触发 · AI 分档退出研究 · 模拟交易门禁'],
  '/ai-chat': ['AI 股票问答', '@股票读取系统数据 · SearXNG 联网 · 流式持久对话'],
  '/candidates': ['候选池', '确定性公式评分与风险过滤结果'],
  '/portfolio': ['模拟组合', '组合权重、行业约束与调仓建议'],
  '/reports': ['研究报告', '研究结论与可追溯证据摘要'],
  '/backtest': ['回测工作台', '固定快照上的事件驱动回测'],
  '/runs': ['运行与审计', '任务状态、失败原因和审计事件'],
  '/about': ['关于本系统', '系统定位、研究链路与使用边界'],
  '/admin': ['用户管理', '账户、角色与访问控制'],
  '/admin/models': ['模型设置', '加密凭据、模型分工、连通性与版本状态'],
}

export function titleForPathname(pathname: string): [string, string] {
  const normalizedPath = pathname === '/' ? '/' : pathname.replace(/\/+$/, '')
  return TITLES[normalizedPath] || TITLES['/']
}

export function AppShell() {
  const { user, logout } = useAuth()
  const { delayed, source, updatedAt } = useMarket()
  const location = useLocation()
  const [title, subtitle] = titleForPathname(location.pathname)
  const isAdmin = user?.role?.toLowerCase() === 'admin'
  const { available, busy, refresh } = useRefreshControl()
  const [menuOpen, setMenuOpen] = useState(false)
  const [clock, setClock] = useState(() => new Date())
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

  useEffect(() => { setMenuOpen(false) }, [location.pathname])

  useEffect(() => {
    const timer = window.setInterval(() => setClock(new Date()), 30_000)
    return () => window.clearInterval(timer)
  }, [])

  const shanghaiClock = clock.toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', weekday: 'short',
    hour: '2-digit', minute: '2-digit', hour12: false,
  })

  return <div className="app-shell">
    <button className={`nav-overlay ${menuOpen ? 'visible' : ''}`} aria-label="关闭导航菜单" tabIndex={menuOpen ? 0 : -1} onClick={() => closeMenu()} />
    <aside className={`sidebar ${menuOpen ? 'open' : ''}`} aria-label="主导航">
      <div className="brand"><span className="brand-mark">霁</span><div><strong>霁衡智研</strong><small>A-SHARE RESEARCH</small></div></div>
      <nav ref={navigation}>
        {NAV.map((item, index) => 'group' in item ? <div className="nav-group" key={index}>{item.group}</div> :
          <NavLink key={item.to} to={item.to!} end={item.end} onClick={() => closeMenu()} className={({ isActive }) => isActive ? 'active' : ''}>
            <span>{item.icon}</span>{item.label}
          </NavLink>)}
        {isAdmin && <><NavLink to="/admin" end onClick={() => closeMenu()} className={({ isActive }) => isActive ? 'active' : ''}><span>⚙</span>用户管理</NavLink><NavLink to="/admin/models" onClick={() => closeMenu()} className={({ isActive }) => isActive ? 'active' : ''}><span>◉</span>模型设置</NavLink></>}
      </nav>
      <div className="sidebar-foot">
        <div className={`feed-state ${delayed ? 'delayed' : ''}`}><i />{delayed ? '行情非实时' : `${source} 行情正常`}</div>
        <small>{updatedAt ? `更新 ${formatTime(updatedAt)}` : '按活跃标的 15 秒刷新'}</small>
      </div>
    </aside>
    <div className="workspace">
      <header className="topbar">
        <button ref={menuButton} className="mobile-menu-button" aria-label="打开导航菜单" aria-expanded={menuOpen} onClick={() => setMenuOpen(true)}>☰</button>
        <div className="topbar-title"><h1>{title}</h1><p>{subtitle}</p></div>
        <div className="top-actions">
          <button className="refresh-button" onClick={() => void refresh()} disabled={!available || busy} title="刷新当前页面">{busy ? '刷新中' : '↻ 刷新'}</button>
          <NotificationBell />
          <ThemeToggle compact />
          <div className="market-clock"><span>上海时间</span><strong>{shanghaiClock}</strong></div>
          <div className="user-menu"><span>{user?.username.slice(0, 1).toUpperCase()}</span><div><strong>{user?.username}</strong><small>{isAdmin ? '管理员' : '研究员'}</small></div><button onClick={() => void logout()} title="退出登录">退出</button></div>
        </div>
      </header>
      {delayed && <div className="stale-banner">上游行情暂不可用，当前展示最近成功缓存。冻结研究与回测快照不受影响。</div>}
      <main className="content"><Outlet /></main>
      <footer className="disclaimer">本系统仅用于研究、回测与模拟组合，不构成投资建议，不接入真实交易。</footer>
    </div>
  </div>
}
