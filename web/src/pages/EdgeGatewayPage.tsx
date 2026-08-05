import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'
import { Navigate } from 'react-router'
import { api } from '../api'
import { ErrorNotice, formatTime, Panel, StatusPill } from '../components/Ui'
import { useAuth } from '../context/AuthContext'
import type { EdgeGatewayConfiguration, EdgeGatewayLogs, EdgeProxyHost, SystemSettings, SystemSettingsDraft } from '../types'

const emptyHost = (): EdgeProxyHost => ({ name: '新入口', domains: ['example.com'], forward_scheme: 'http', forward_host: 'web', forward_port: 80, ssl_enabled: true, websocket_support: true, enabled: true, notes: '' })
const starterToml = `serverAddr = "frps.example.com"\nserverPort = 7000\n\nauth.method = "token"\nauth.token = "replace-with-a-secret-token"\n\n[[proxies]]\nname = "ashare-edge-http"\ntype = "http"\nlocalIP = "127.0.0.1"\nlocalPort = 80\ncustomDomains = ["research.example.com"]\n`
const EDGE_SETTING_KEYS = ['edge_domain', 'edge_acme_email', 'edge_acme_ca_server', 'edge_frpc_enabled', 'edge_frpc_config_file']

function settingValue(settings: SystemSettings | null, key: string): string | boolean {
  return settings?.values[key] as string | boolean || (key === 'edge_frpc_enabled' ? false : '')
}

export function EdgeGatewayPage() {
  const { user } = useAuth()
  const isAdmin = user?.role?.toLowerCase() === 'admin'
  const [config, setConfig] = useState<EdgeGatewayConfiguration | null>(null)
  const [settings, setSettings] = useState<SystemSettings | null>(null)
  const [settingsForm, setSettingsForm] = useState<SystemSettingsDraft>({})
  const [frpc, setFrpc] = useState('')
  const [hosts, setHosts] = useState<EdgeProxyHost[]>([])
  const [enabled, setEnabled] = useState(false)
  const [validationMode, setValidationMode] = useState<'STRICT' | 'COMPATIBLE'>('STRICT')
  const [unlockToken, setUnlockToken] = useState('')
  const [password, setPassword] = useState('')
  const [showUnlock, setShowUnlock] = useState(false)
  const [busy, setBusy] = useState(false)
  const [settingsBusy, setSettingsBusy] = useState(false)
  const [logs, setLogs] = useState<EdgeGatewayLogs | null>(null)
  const [logsBusy, setLogsBusy] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')

  const load = useCallback(async (token = '') => {
    const [next, nextSettings] = await Promise.all([
      api.edgeGateway(token || undefined),
      api.systemSettings(),
    ])
    setConfig(next)
    setEnabled(next.enabled)
    setValidationMode(next.validation_mode || 'STRICT')
    setHosts(next.proxy_hosts)
    setSettings(nextSettings)
    setSettingsForm({ ...nextSettings.values })
    if (token) setFrpc(next.frpc_toml || starterToml)
  }, [])

  const loadLogs = useCallback(async () => {
    setLogsBusy(true)
    try {
      setLogs(await api.edgeGatewayLogs())
    } catch (reason) {
      setLogs({ available: false, message: reason instanceof Error ? reason.message : 'FRP 日志加载失败', lines: [] })
    } finally {
      setLogsBusy(false)
    }
  }, [])

  useEffect(() => {
    if (!isAdmin) return
    void load().catch((reason) => setError(reason instanceof Error ? reason.message : '加载失败'))
  }, [isAdmin, load])

  useEffect(() => {
    if (!isAdmin) return
    void loadLogs()
    const timer = window.setInterval(() => void loadLogs(), 10000)
    return () => window.clearInterval(timer)
  }, [isAdmin, loadLogs])

  const lines = useMemo(() => frpc.split('\n'), [frpc])

  function updateHost(index: number, patch: Partial<EdgeProxyHost>) {
    setHosts((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, ...patch } : item))
  }

  function changedSettings(): SystemSettingsDraft {
    const changed: SystemSettingsDraft = {}
    for (const key of EDGE_SETTING_KEYS) {
      if (String(settingsForm[key] ?? '') !== String(settings?.values[key] ?? '')) changed[key] = settingsForm[key]
    }
    return changed
  }

  async function unlock(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('')
    try {
      const result = await api.unlockSystemSettings(password)
      setUnlockToken(result.unlock_token); setPassword(''); setShowUnlock(false)
      await load(result.unlock_token)
    } catch (reason) { setError(reason instanceof Error ? reason.message : '解锁失败') } finally { setBusy(false) }
  }

  async function saveGatewaySettings() {
    if (!unlockToken) { setShowUnlock(true); return }
    setSettingsBusy(true); setError(''); setMessage('')
    try {
      const payload = changedSettings()
      if (!Object.keys(payload).length) { setMessage('没有需要保存的网关参数'); return }
      const next = await api.saveSystemSettings(payload, unlockToken)
      setSettings(next); setSettingsForm({ ...next.values }); setMessage(`公网网关参数已保存。${next.restart_required ? '等待本机控制器应用。' : ''}`)
    } catch (reason) { setError(reason instanceof Error ? reason.message : '公网网关参数保存失败') } finally { setSettingsBusy(false) }
  }

  async function validate() {
    setBusy(true); setError(''); setMessage('')
    try {
      const result = await api.validateEdgeGateway({ validation_mode: validationMode, proxy_hosts: hosts, frpc_toml: frpc })
      setMessage(`配置有效 · ${result.proxy_count} 个入口 · Nginx ${result.nginx_sha256.slice(0, 12)}`)
    } catch (reason) { setError(reason instanceof Error ? reason.message : '校验失败') } finally { setBusy(false) }
  }

  async function save(event: FormEvent) {
    event.preventDefault(); if (!unlockToken) { setShowUnlock(true); return }
    setBusy(true); setError(''); setMessage('')
    try {
      const next = await api.saveEdgeGateway({ enabled, validation_mode: validationMode, proxy_hosts: hosts, frpc_toml: frpc }, unlockToken)
      setConfig(next); setMessage('网关配置已保存，等待本机控制器应用'); void loadLogs()
    } catch (reason) { setError(reason instanceof Error ? reason.message : '保存失败') } finally { setBusy(false) }
  }

  async function rollback() {
    if (!unlockToken || !window.confirm('回滚到上一版 edge-gateway 配置？')) return
    setBusy(true); setError('')
    try {
      const next = await api.rollbackEdgeGateway(unlockToken)
      setConfig(next); setEnabled(next.enabled); setValidationMode(next.validation_mode || 'STRICT'); setHosts(next.proxy_hosts); setFrpc(next.frpc_toml); setMessage('已回滚，等待本机拓扑控制器应用')
    } catch (reason) { setError(reason instanceof Error ? reason.message : '回滚失败') } finally { setBusy(false) }
  }

  if (!isAdmin) return <Navigate to="/" replace />
  return <div className="page-stack edge-gateway-page">
    <section className="hero-strip"><div><span className="eyebrow">EDGE GATEWAY CONTROL</span><h2>公网边缘网关</h2><p>域名、证书、FRP 隧道和网站入口统一在这里管理。</p></div><div className="hero-stat"><span>配置版本</span><strong>{config?.version || 0}</strong><small>{config?.config_sha256 ? config.config_sha256.slice(0, 12) : '尚未配置'}</small></div><div className="hero-stat"><span>应用状态</span><strong><StatusPill status={config?.apply_status === 'APPLIED' ? 'ACTIVE' : config?.apply_status === 'FAILED' ? 'ERROR' : 'PENDING'} /></strong><small>{config?.applied_at || '等待控制器'}</small></div></section>
    <ErrorNotice message={error} />{message && <div className="snapshot-isolation">{message}</div>}{config?.source_sync && <div className="snapshot-isolation">已读取部署目录中的最新文件；外部修改会在下一次控制器轮询时导入。</div>}

    <Panel title="公网网关参数" eyebrow="PUBLIC EDGE SETTINGS" action={<button type="button" className="primary" onClick={() => void saveGatewaySettings()} disabled={settingsBusy}>{settingsBusy ? '保存中…' : '保存网关参数'}</button>}>
      <div className="edge-settings-grid">
        <label>公网域名<input disabled={!unlockToken || settingsBusy} value={String(settingsForm.edge_domain ?? '')} placeholder="research.example.com" onChange={(event) => setSettingsForm((previous) => ({ ...previous, edge_domain: event.target.value }))} /><small className="form-hint">浏览器访问的域名，DNS 必须指向本服务器。</small></label>
        <label>证书申请邮箱<input disabled={!unlockToken || settingsBusy} type="email" value={String(settingsForm.edge_acme_email ?? '')} placeholder="admin@example.com" onChange={(event) => setSettingsForm((previous) => ({ ...previous, edge_acme_email: event.target.value }))} /><small className="form-hint">用于 HTTPS 证书申请和到期通知。</small></label>
        <label>证书服务<select disabled={!unlockToken || settingsBusy} value={String(settingsForm.edge_acme_ca_server || 'letsencrypt')} onChange={(event) => setSettingsForm((previous) => ({ ...previous, edge_acme_ca_server: event.target.value }))}><option value="letsencrypt">Let's Encrypt（正式）</option><option value="letsencrypt_test">Let's Encrypt（测试）</option></select></label>
        <label className="system-checkbox">连接外部 FRP<input disabled={!unlockToken || settingsBusy} type="checkbox" checked={Boolean(settingsForm.edge_frpc_enabled)} onChange={(event) => setSettingsForm((previous) => ({ ...previous, edge_frpc_enabled: event.target.checked }))} /><small className="form-hint">需要把本机网站通过已有的 frps 对外暴露时开启。</small></label>
        <label>FRP 配置文件路径<input disabled={!unlockToken || settingsBusy} value={String(settingsForm.edge_frpc_config_file ?? '')} placeholder="./.secrets/edge-frpc.toml" onChange={(event) => setSettingsForm((previous) => ({ ...previous, edge_frpc_config_file: event.target.value }))} /><small className="form-hint">宿主机上的私有配置文件，不要把 Token 提交到 Git。</small></label>
      </div>
      {!unlockToken && <p className="form-hint">点击下方编辑器旁的“解锁编辑”后，才能修改网关参数。</p>}
    </Panel>

    <form onSubmit={save} className="edge-gateway-layout">
      <Panel title="FRP 客户端配置" eyebrow="FRPC CONFIGURATION" action={<div className="row-actions"><button type="button" className="secondary" onClick={() => void validate()} disabled={busy}>校验</button><button type="submit" className="primary" disabled={busy}>{busy ? '保存中…' : '保存配置'}</button></div>}>
        <div className="editor-toolbar"><div><span>frpc.toml</span><span className="editor-lock-state">{unlockToken ? '已解锁 · 服务端加密保存' : '锁定 · 点击解锁后编辑'}</span></div><button type="button" className="secondary editor-unlock-button" onClick={() => setShowUnlock(true)} disabled={Boolean(unlockToken) || busy}>{unlockToken ? '已解锁' : '解锁编辑'}</button></div>
        <div className={`code-editor ${!unlockToken ? 'locked' : ''}`}><div className="line-numbers" aria-hidden="true">{lines.map((_, index) => <span key={index}>{index + 1}</span>)}</div><textarea aria-label="FRP TOML" spellCheck={false} value={frpc} readOnly={!unlockToken} placeholder={unlockToken ? starterToml : '管理员解锁后加载加密配置'} onChange={(event) => setFrpc(event.target.value)} /></div>
        <div className="edge-validation-row"><label className="system-checkbox">启用严格 FRP 校验<input type="checkbox" checked={validationMode === 'STRICT'} onChange={(event) => setValidationMode(event.target.checked ? 'STRICT' : 'COMPATIBLE')} /><small className="form-hint">关闭后允许旧版 snake_case 字段；仍检查 TOML 语法、回环目标和 80/443 端口。</small></label><span className="form-hint">{validationMode === 'STRICT' ? '推荐给新配置' : '兼容老版本配置'}</span></div>
        <p className="form-hint">Token 只会加密保存；日志只显示脱敏后的运行信息。</p>
      </Panel>

      <Panel title="网站访问入口" eyebrow="WEBSITE ENTRANCES" action={<button type="button" className="secondary" onClick={() => setHosts((items) => [...items, emptyHost()])} disabled={hosts.length >= 32}>添加入口</button>}>
        <p className="form-hint proxy-host-intro">每个入口就是一个访问域名。这里不需要编写 Nginx 语句，只需填写域名和应用端口。</p>
        <label className="system-checkbox">启用公网网关<input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} /><small className="form-hint">关闭后控制器停止网关，不删除证书或历史配置。</small></label>
        <div className="proxy-host-list">{hosts.length ? hosts.map((host, index) => <article className="proxy-host-card" key={host.id || index}><header><div><strong>{host.domains[0] || `入口 ${index + 1}`}</strong><small>{host.enabled ? '已启用' : '已停用'} · {host.forward_scheme.toUpperCase()} · 应用端口 {host.forward_port}</small></div><button type="button" className="danger-button" onClick={() => setHosts((items) => items.filter((_, itemIndex) => itemIndex !== index))}>删除入口</button></header><div className="proxy-host-grid"><label className="wide">访问域名<input value={host.domains.join(',')} placeholder="research.example.com" onChange={(event) => updateHost(index, { domains: event.target.value.split(',').map((value) => value.trim()).filter(Boolean) })} /><small className="form-hint">多个域名用逗号分隔。</small></label><label>应用协议<select value={host.forward_scheme} onChange={(event) => updateHost(index, { forward_scheme: event.target.value as 'http' | 'https' })}><option value="http">HTTP</option><option value="https">HTTPS</option></select></label><label>应用端口<input type="number" min={1} max={65535} value={host.forward_port} onChange={(event) => updateHost(index, { forward_port: Number(event.target.value) })} /><small className="form-hint">Docker 默认应用端口为 80。</small></label><label className="system-checkbox">自动 HTTPS<input type="checkbox" checked={host.ssl_enabled} onChange={(event) => updateHost(index, { ssl_enabled: event.target.checked })} /><small className="form-hint">需要证书；HTTP 会跳转到 HTTPS。</small></label><label className="system-checkbox">支持实时连接<input type="checkbox" checked={host.websocket_support} onChange={(event) => updateHost(index, { websocket_support: event.target.checked })} /><small className="form-hint">聊天、推送等 WebSocket 场景需要开启。</small></label><label className="system-checkbox">启用此入口<input type="checkbox" checked={host.enabled} onChange={(event) => updateHost(index, { enabled: event.target.checked })} /></label><label>应用服务名<input value={host.forward_host} placeholder="web" onChange={(event) => updateHost(index, { forward_host: event.target.value })} /><small className="form-hint">Docker 默认填 web；仅允许安全白名单目标。</small></label></div></article>) : <div className="empty-state">暂无入口，点击“添加入口”开始配置。</div>}</div>
      </Panel>
    </form>

    <Panel title="FRP 运行日志" eyebrow="FRP SERVICE LOG" action={<button type="button" className="secondary" onClick={() => void loadLogs()} disabled={logsBusy}>{logsBusy ? '读取中…' : '刷新日志'}</button>}>
      <div className="edge-log-status"><StatusPill status={logs?.available ? 'ACTIVE' : 'PENDING'} /><span>{logs?.updated_at ? `更新于 ${formatTime(logs.updated_at)}` : '当前部署没有可读取的 FRP 日志'}</span></div>
      {logs?.lines.length ? <pre className="edge-log-output">{logs.lines.join('\n')}</pre> : <div className="empty-state"><strong>{logs?.message || '正在读取 FRP 日志'}</strong><p>开启外部 FRP 并由控制器启动网关后，这里会自动显示最近运行信息。</p></div>}
    </Panel>

    <Panel title="版本与回滚" eyebrow="VERSION CONTROL"><div className="advanced-actions"><div><strong>{unlockToken ? '管理员编辑权限已启用' : '只读监控模式'}</strong><small>FRP Token 只在解锁会话中显示，服务端始终加密保存。</small></div><div className="row-actions"><button type="button" className="danger-button" onClick={() => void rollback()} disabled={!unlockToken || busy || (config?.version || 0) < 2}>回滚上一版</button></div></div></Panel>
    {showUnlock && <div className="automatic-settings-overlay"><form className="system-unlock-dialog" onSubmit={unlock}><h2>解锁 Edge Gateway 配置</h2><p>请输入当前管理员密码，解锁窗口有效期 10 分钟。</p><label>管理员密码<input autoFocus type="password" value={password} onChange={(event) => setPassword(event.target.value)} required /></label><div className="row-actions"><button type="button" className="secondary" onClick={() => setShowUnlock(false)}>取消</button><button className="primary" disabled={busy}>验证并解锁</button></div></form></div>}
  </div>
}
