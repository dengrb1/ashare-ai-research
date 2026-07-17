import { useState, type FormEvent } from 'react'
import { useAuth } from '../context/AuthContext'
import { ThemeToggle } from '../context/ThemeContext'

export function LoginPage() {
  const { login } = useAuth()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)

  async function submit(event: FormEvent) {
    event.preventDefault()
    setSubmitting(true)
    setError('')
    try {
      await login(username.trim(), password)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '登录失败，请检查账号和密码')
    } finally {
      setSubmitting(false)
    }
  }

  return <div className="login-page">
    <div className="login-theme"><ThemeToggle /></div>
    <div className="login-atmosphere"><div className="grid-glow" /><div className="market-lines" /></div>
    <section className="login-copy">
      <div className="brand login-brand"><span className="brand-mark">霁</span><div><strong>霁衡智研</strong><small>A-SHARE INTELLIGENCE</small></div></div>
      <div><span className="kicker">POINT-IN-TIME · DETERMINISTIC · AUDITABLE</span><h1>在噪声之上，<br />建立可验证的判断。</h1><p>面向 A 股研究团队的多用户投研与回测终端。实时行情与冻结快照严格隔离，每个结论都可复现、可追溯。</p></div>
      <div className="login-features"><span>后复权统一口径</span><span>版本化确定性评分</span><span>A 股规则生效日管理</span></div>
    </section>
    <section className="login-card-wrap">
      <form className="login-card" onSubmit={submit}>
        <div><span className="eyebrow">SECURE ACCESS</span><h2>登录研究终端</h2><p>仅管理员创建的内部账户可以访问</p></div>
        <label>用户名<input autoFocus autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} placeholder="请输入用户名" required /></label>
        <label>密码<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="请输入密码" required /></label>
        {error && <div className="notice error">{error}</div>}
        <button className="primary large" disabled={submitting}>{submitting ? '正在验证…' : '进入终端'}<span>→</span></button>
        <small className="security-note">会话使用 HttpOnly Cookie · CSRF 防护 · Argon2 密码哈希</small>
      </form>
    </section>
  </div>
}
