import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { Navigate } from 'react-router'
import { api } from '../api'
import { ErrorNotice, Loading, Panel, StatusPill } from '../components/Ui'
import { useAuth } from '../context/AuthContext'
import type { EdgeGatewayConfiguration, EdgeProxyHost } from '../types'

const emptyHost = (): EdgeProxyHost => ({ name: '新代理', domains: ['example.com'], forward_scheme: 'http', forward_host: 'web', forward_port: 80, ssl_enabled: true, websocket_support: true, enabled: true, notes: '' })
const starterToml = `serverAddr = "frps.example.com"\nserverPort = 7000\n\nauth.method = "token"\nauth.token = "replace-with-a-secret-token"\n\n[[proxies]]\nname = "ashare-edge-http"\ntype = "http"\nlocalIP = "127.0.0.1"\nlocalPort = 80\ncustomDomains = ["research.example.com"]\n`

export function EdgeGatewayPage() {
  const { user } = useAuth()
  const isAdmin = user?.role?.toLowerCase() === 'admin'
  const [config, setConfig] = useState<EdgeGatewayConfiguration | null>(null)
  const [frpc, setFrpc] = useState('')
  const [hosts, setHosts] = useState<EdgeProxyHost[]>([])
  const [enabled, setEnabled] = useState(false)
  const [unlockToken, setUnlockToken] = useState('')
  const [password, setPassword] = useState('')
  const [showUnlock, setShowUnlock] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')

  const load = async (token = '') => {
    const next = await api.edgeGateway(token || undefined)
    setConfig(next); setEnabled(next.enabled); setHosts(next.proxy_hosts); if (token) setFrpc(next.frpc_toml || starterToml)
  }
  useEffect(() => { if (isAdmin) void load().catch((reason) => setError(reason instanceof Error ? reason.message : '加载失败')) }, [isAdmin])
  const lines = useMemo(() => frpc.split('\n'), [frpc])

  async function unlock(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('')
    try { const result = await api.unlockSystemSettings(password); setUnlockToken(result.unlock_token); setPassword(''); setShowUnlock(false); await load(result.unlock_token) } catch (reason) { setError(reason instanceof Error ? reason.message : '解锁失败') } finally { setBusy(false) }
  }
  async function validate() {
    setBusy(true); setError(''); setMessage('')
    try { const result = await api.validateEdgeGateway({ proxy_hosts: hosts, frpc_toml: frpc }); setMessage(`配置有效 · ${result.proxy_count} 个代理 · Nginx ${result.nginx_sha256.slice(0, 12)}`) } catch (reason) { setError(reason instanceof Error ? reason.message : '校验失败') } finally { setBusy(false) }
  }
  async function save(event: FormEvent) {
    event.preventDefault(); if (!unlockToken) { setShowUnlock(true); return }
    setBusy(true); setError(''); setMessage('')
    try { const next = await api.saveEdgeGateway({ enabled, proxy_hosts: hosts, frpc_toml: frpc }, unlockToken); setConfig(next); setMessage('已保存，等待本机拓扑控制器应用') } catch (reason) { setError(reason instanceof Error ? reason.message : '保存失败') } finally { setBusy(false) }
  }
  async function rollback() {
    if (!unlockToken || !window.confirm('回滚到上一版 edge-gateway 配置？')) return
    setBusy(true); setError('')
    try { const next = await api.rollbackEdgeGateway(unlockToken); setConfig(next); setEnabled(next.enabled); setHosts(next.proxy_hosts); setFrpc(next.frpc_toml); setMessage('已回滚，等待本机拓扑控制器应用') } catch (reason) { setError(reason instanceof Error ? reason.message : '回滚失败') } finally { setBusy(false) }
  }
  if (!isAdmin) return <Navigate to="/" replace />
  return <div className="page-stack edge-gateway-page">
    <section className="hero-strip"><div><span className="eyebrow">EDGE GATEWAY CONTROL</span><h2>公网边缘网关</h2><p>集中管理 FRP 隧道与受限 Nginx 代理主机。保存后由本机控制器校验并应用。</p></div><div className="hero-stat"><span>配置版本</span><strong>{config?.version || 0}</strong><small>{config?.config_sha256 ? config.config_sha256.slice(0, 12) : '尚未配置'}</small></div><div className="hero-stat"><span>应用状态</span><strong><StatusPill status={config?.apply_status === 'APPLIED' ? 'ACTIVE' : config?.apply_status === 'FAILED' ? 'ERROR' : 'PENDING'} /></strong><small>{config?.applied_at || '等待控制器'}</small></div></section>
    <ErrorNotice message={error} />{message && <div className="snapshot-isolation">{message}</div>}
    <form onSubmit={save} className="edge-gateway-layout">
      <Panel title="FRP 客户端 TOML" eyebrow="FRPC CONFIGURATION" action={<div className="row-actions"><button type="button" className="secondary" onClick={() => void validate()} disabled={busy}>校验</button><button type="submit" className="primary" disabled={busy}>{busy ? '保存中…' : '保存配置'}</button></div>}>
        <div className="editor-toolbar"><span>frpc.toml</span><span>{unlockToken ? '已解锁 · 服务端加密保存' : '锁定 · 点击解锁后编辑'}</span></div>
        <div className={`code-editor ${!unlockToken ? 'locked' : ''}`}><div className="line-numbers" aria-hidden="true">{lines.map((_, index) => <span key={index}>{index + 1}</span>)}</div><textarea aria-label="FRP TOML" spellCheck={false} value={frpc} readOnly={!unlockToken} placeholder={unlockToken ? starterToml : '管理员解锁后加载加密配置'} onChange={(event) => setFrpc(event.target.value)} /></div>
        <p className="form-hint">上限 64 KiB；FRP 代理只能连接同一网关容器的 127.0.0.1:80 或 443。</p>
      </Panel>
      <Panel title="Nginx 代理主机" eyebrow="PROXY HOSTS" action={<button type="button" className="secondary" onClick={() => setHosts((items) => [...items, emptyHost()])} disabled={hosts.length >= 32}>添加代理</button>}>
        <label className="system-checkbox">启用 edge-gateway<input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} /><small className="form-hint">关闭时控制器停止网关，不删除证书或历史配置。</small></label>
        <div className="proxy-host-list">{hosts.length ? hosts.map((host, index) => <article className="proxy-host-card" key={host.id || index}><header><div><strong>{host.name}</strong><small>{host.domains.join(' · ')}</small></div><button type="button" className="danger-button" onClick={() => setHosts((items) => items.filter((_, itemIndex) => itemIndex !== index))}>删除</button></header><div className="proxy-host-grid"><label>名称<input value={host.name} onChange={(event) => setHosts((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, name: event.target.value } : item))} /></label><label>域名（逗号分隔）<input value={host.domains.join(',')} onChange={(event) => setHosts((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, domains: event.target.value.split(',').map((value) => value.trim()).filter(Boolean) } : item))} /></label><label>目标主机<input value={host.forward_host} onChange={(event) => setHosts((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, forward_host: event.target.value } : item))} /></label><label>目标端口<input type="number" min={1} max={65535} value={host.forward_port} onChange={(event) => setHosts((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, forward_port: Number(event.target.value) } : item))} /></label><label>协议<select value={host.forward_scheme} onChange={(event) => setHosts((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, forward_scheme: event.target.value as 'http' | 'https' } : item))}><option value="http">HTTP</option><option value="https">HTTPS</option></select></label><label className="system-checkbox">SSL<input type="checkbox" checked={host.ssl_enabled} onChange={(event) => setHosts((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, ssl_enabled: event.target.checked } : item))} /></label><label className="system-checkbox">WebSocket<input type="checkbox" checked={host.websocket_support} onChange={(event) => setHosts((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, websocket_support: event.target.checked } : item))} /></label><label className="system-checkbox">启用<input type="checkbox" checked={host.enabled} onChange={(event) => setHosts((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, enabled: event.target.checked } : item))} /></label></div></article>) : <div className="empty-state">暂无代理主机，点击“添加代理”开始配置。</div>}</div>
      </Panel>
    </form>
    <Panel title="配置操作" eyebrow="VERSION CONTROL"><div className="advanced-actions"><div><strong>{unlockToken ? '管理员编辑权限已启用' : '只读监控模式'}</strong><small>FRP token 只在解锁会话中显示，服务端始终加密保存。</small></div><div className="row-actions"><button type="button" className="secondary" onClick={() => setShowUnlock(true)} disabled={Boolean(unlockToken)}>解锁编辑</button><button type="button" className="danger-button" onClick={() => void rollback()} disabled={!unlockToken || busy || (config?.version || 0) < 2}>回滚上一版</button></div></div></Panel>
    {showUnlock && <div className="automatic-settings-overlay"><form className="system-unlock-dialog" onSubmit={unlock}><h2>解锁 edge-gateway 配置</h2><p>请输入当前管理员密码，解锁窗口有效期 10 分钟。</p><label>管理员密码<input autoFocus type="password" value={password} onChange={(event) => setPassword(event.target.value)} required /></label><div className="row-actions"><button type="button" className="secondary" onClick={() => setShowUnlock(false)}>取消</button><button className="primary" disabled={busy}>验证并解锁</button></div></form></div>}
  </div>
}
