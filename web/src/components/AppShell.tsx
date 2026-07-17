import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { useMarket } from '../context/MarketContext'
import { formatTime } from './Ui'
import { ThemeToggle } from '../context/ThemeContext'

const NAV = [
  { to: '/', label: '全局仪表盘', icon: '◫', end: true },
  { to: '/market', label: '行情与 K 线', icon: '⌁' },
  { to: '/search', label: '金融数据搜索', icon: '⌕' },
  { to: '/assets', label: '自选与持仓', icon: '◇' },
  { group: '研究中心' },
  { to: '/research', label: '每日研究', icon: '✦' },
  { to: '/candidates', label: '候选池', icon: '◎' },
  { to: '/portfolio', label: '模拟组合', icon: '▥' },
  { to: '/reports', label: '研究报告', icon: '▤' },
  { group: '任务与系统' },
  { to: '/backtest', label: '回测工作台', icon: '⟲' },
  { to: '/runs', label: '运行与审计', icon: '≡' },
]

const TITLES: Record<string, [string, string]> = {
  '/': ['全局仪表盘', '研究运行、市场状态与组合概览'],
  '/market': ['行情与 K 线', '活跃标的按需刷新 · 历史序列后复权'],
  '/search': ['金融数据搜索', 'NeoData 自然语言金融查询 · 默认搜索源'],
  '/assets': ['自选与持仓', '关注列表与模拟持仓实时状态'],
  '/research': ['每日研究', '基于冻结快照的可复现异步研究'],
  '/candidates': ['候选池', '确定性公式评分与风险过滤结果'],
  '/portfolio': ['模拟组合', '组合权重、行业约束与调仓建议'],
  '/reports': ['研究报告', '研究结论与可追溯证据摘要'],
  '/backtest': ['回测工作台', '固定快照上的事件驱动回测'],
  '/runs': ['运行与审计', '任务状态、失败原因和审计事件'],
  '/admin': ['用户管理', '账户、角色与访问控制'],
}

export function AppShell() {
  const { user, logout } = useAuth()
  const { delayed, source, updatedAt } = useMarket()
  const location = useLocation()
  const [title, subtitle] = TITLES[location.pathname] || TITLES['/']
  const isAdmin = user?.role?.toLowerCase() === 'admin'

  return <div className="app-shell">
    <aside className="sidebar">
      <div className="brand"><span className="brand-mark">霁</span><div><strong>霁衡智研</strong><small>A-SHARE RESEARCH</small></div></div>
      <nav>
        {NAV.map((item, index) => 'group' in item ? <div className="nav-group" key={index}>{item.group}</div> :
          <NavLink key={item.to} to={item.to!} end={item.end} className={({ isActive }) => isActive ? 'active' : ''}>
            <span>{item.icon}</span>{item.label}
          </NavLink>)}
        {isAdmin && <><div className="nav-group">管理</div><NavLink to="/admin" className={({ isActive }) => isActive ? 'active' : ''}><span>⚙</span>用户管理</NavLink></>}
      </nav>
      <div className="sidebar-foot">
        <div className={`feed-state ${delayed ? 'delayed' : ''}`}><i />{delayed ? '行情非实时' : `${source} 行情正常`}</div>
        <small>{updatedAt ? `更新 ${formatTime(updatedAt)}` : '按活跃标的 15 秒刷新'}</small>
      </div>
    </aside>
    <div className="workspace">
      <header className="topbar">
        <div><h1>{title}</h1><p>{subtitle}</p></div>
        <div className="top-actions">
          <ThemeToggle compact />
          <div className="market-clock"><span>沪深交易时段</span><strong>{new Date().toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit', weekday: 'short' })}</strong></div>
          <div className="user-menu"><span>{user?.username.slice(0, 1).toUpperCase()}</span><div><strong>{user?.username}</strong><small>{isAdmin ? '管理员' : '研究员'}</small></div><button onClick={() => void logout()} title="退出登录">退出</button></div>
        </div>
      </header>
      {delayed && <div className="stale-banner">上游行情暂不可用，当前展示最近成功缓存。冻结研究与回测快照不受影响。</div>}
      <main className="content"><Outlet /></main>
      <footer className="disclaimer">本系统仅用于研究、回测与模拟组合，不构成投资建议，不接入真实交易。</footer>
    </div>
  </div>
}
