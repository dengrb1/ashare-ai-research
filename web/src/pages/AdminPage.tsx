import { useEffect, useState, type FormEvent } from 'react'
import { Navigate } from 'react-router'
import { api } from '../api'
import { useAuth } from '../context/AuthContext'
import type { User } from '../types'
import { Empty, ErrorNotice, formatTime, Loading, Panel, StatusPill } from '../components/Ui'

export function AdminPage() {
  const { user: current } = useAuth()
  const [users, setUsers] = useState<User[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [form, setForm] = useState({ username: '', password: '', role: 'USER' })
  const [submitting, setSubmitting] = useState(false)
  const isAdmin = current?.role?.toLowerCase() === 'admin'
  const load = () => api.users().then(setUsers).catch((reason) => setError(reason instanceof Error ? reason.message : '用户列表加载失败')).finally(() => setLoading(false))
  useEffect(() => { if (isAdmin) void load() }, [isAdmin])
  if (!isAdmin) return <Navigate to="/" replace />

  async function create(event: FormEvent) {
    event.preventDefault(); setSubmitting(true); setError('')
    try { await api.createUser(form); setForm({ username: '', password: '', role: 'USER' }); await load() } catch (reason) { setError(reason instanceof Error ? reason.message : '创建用户失败') } finally { setSubmitting(false) }
  }
  async function toggle(user: User) { const id = user.id || user.user_id || user.username; try { await api.setUserDisabled(id, !(user.disabled || user.is_active === false || user.enabled === false)); await load() } catch (reason) { setError(reason instanceof Error ? reason.message : '更新账户失败') } }
  async function reset(user: User) { const password = window.prompt(`为 ${user.username} 设置新密码（至少 10 位）`); if (!password) return; try { await api.resetPassword(user.id || user.user_id || user.username, password); window.alert('密码已重置，已有会话将失效。') } catch (reason) { setError(reason instanceof Error ? reason.message : '密码重置失败') } }
  async function remove(user: User) { if (!window.confirm(`确定删除用户 ${user.username}？该用户的会话、聊天、图片与个人档案将被清除，其历史研究报告将匿名保留。`)) return; const id = user.id || user.user_id || user.username; try { await api.deleteUser(id); await load() } catch (reason) { setError(reason instanceof Error ? reason.message : '删除用户失败') } }

  return <div className="admin-layout">
    <Panel title="创建内部账户" eyebrow="NEW USER">
      <form className="run-form" onSubmit={create}><label>用户名<input value={form.username} onChange={(event) => setForm({ ...form, username: event.target.value })} minLength={3} required /></label><label>初始密码<input type="password" value={form.password} onChange={(event) => setForm({ ...form, password: event.target.value })} minLength={10} required /></label><label>角色<select value={form.role} onChange={(event) => setForm({ ...form, role: event.target.value })}><option value="USER">研究员</option><option value="ADMIN">管理员</option></select></label><button className="primary large" disabled={submitting}>{submitting ? '创建中…' : '创建账户'}</button><p className="form-hint">系统无公开注册入口。管理员可创建、禁用、删除和重置账户；内置管理员账户不可删除或禁用，其密码仅可由本人修改。</p></form>
    </Panel>
    <Panel title="账户与角色" eyebrow="ACCESS CONTROL" action={<span className="count-badge">{users.length}</span>}>
      <ErrorNotice message={error} />
      {loading ? <Loading /> : users.length ? <div className="table-wrap"><table><thead><tr><th>用户</th><th>角色</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead><tbody>{users.map((user) => { const disabled = user.disabled || user.is_active === false || user.enabled === false; const isSelf = user.username === current?.username; const protectedAccount = Boolean(user.is_admin_account); return <tr key={user.id || user.user_id || user.username}><td><strong>{user.username}</strong>{protectedAccount ? <small>内置管理员</small> : <small>{user.id || user.user_id || '内置账户'}</small>}</td><td>{user.role.toUpperCase() === 'ADMIN' ? '管理员' : '研究员'}</td><td><StatusPill status={disabled ? 'DISABLED' : 'ACTIVE'} /></td><td>{formatTime(user.created_at)}</td><td><div className="row-actions">{!(protectedAccount && !isSelf) && <button className="secondary" onClick={() => void reset(user)}>重置密码</button>}<button className={disabled ? 'secondary' : 'danger-button'} disabled={protectedAccount || isSelf} onClick={() => void toggle(user)}>{disabled ? '启用' : '禁用'}</button>{!(protectedAccount || isSelf) && <button className="danger-button" onClick={() => void remove(user)}>删除</button>}</div></td></tr>})}</tbody></table></div> : <Empty title="暂无用户" />}
    </Panel>
  </div>
}
