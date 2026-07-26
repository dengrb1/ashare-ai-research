import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { Cpu, Eye, EyeOff, HardDrive, LockKeyhole, MemoryStick, RefreshCw, Server, ShieldCheck, UnlockKeyhole, X } from 'lucide-react'
import { Link, Navigate } from 'react-router-dom'
import { ApiError, api } from '../api'
import { ErrorNotice, formatTime, Loading, Panel, StatusPill } from '../components/Ui'
import { useAuth } from '../context/AuthContext'
import type { SystemResources, SystemSettings, SystemSettingsDraft } from '../types'

type FieldKind = 'number' | 'text' | 'checkbox'
type Field = { key: string; label: string; kind: FieldKind; hint?: string }

const STORAGE_FIELDS: Field[] = [
  { key: 'object_store_endpoint', label: '对象存储 Endpoint', kind: 'text' },
  { key: 'object_store_bucket', label: 'Bucket', kind: 'text' },
  { key: 'object_store_secure', label: '启用 TLS', kind: 'checkbox' },
]
const SEARCH_MARKET_FIELDS: Field[] = [
  { key: 'searxng_base_url', label: 'SearXNG 地址', kind: 'text' },
  { key: 'searxng_timeout_seconds', label: 'SearXNG 超时（秒）', kind: 'number' },
  { key: 'searxng_max_results', label: 'SearXNG 结果数', kind: 'number' },
  { key: 'market_cache_seconds', label: '行情缓存（秒）', kind: 'number' },
  { key: 'market_kline_cache_seconds', label: 'K 线缓存（秒）', kind: 'number' },
  { key: 'market_prefetch_max_workers', label: '行情预取并发', kind: 'number' },
  { key: 'market_provider_max_workers', label: '供应商并发', kind: 'number' },
  { key: 'market_provider_max_queue', label: '供应商队列', kind: 'number' },
  { key: 'market_cache_max_entries', label: '行情缓存条目', kind: 'number' },
  { key: 'market_stale_seconds', label: '行情陈旧阈值（秒）', kind: 'number' },
  { key: 'market_timeout_seconds', label: '行情请求超时（秒）', kind: 'number' },
  { key: 'financial_search_cache_seconds', label: '金融搜索缓存（秒）', kind: 'number' },
  { key: 'financial_search_max_concurrency', label: '金融搜索并发', kind: 'number' },
  { key: 'financial_search_rate_limit_per_minute', label: '金融搜索限流（每分钟）', kind: 'number' },
]
const RESEARCH_FIELDS: Field[] = [
  { key: 'daily_research_start_hour', label: '每日研究启动小时', kind: 'number' },
  { key: 'daily_research_start_minute', label: '每日研究启动分钟', kind: 'number' },
  { key: 'daily_research_retry_minutes', label: '数据就绪重试间隔（分钟）', kind: 'number' },
  { key: 'daily_research_retry_limit_minutes', label: '旧版数据就绪窗口（兼容）', kind: 'number', hint: '保留旧配置读取；自动补数统一在下一交易日 09:25 截止。' },
  { key: 'worker_lease_seconds', label: 'Worker 租约（秒）', kind: 'number' },
  { key: 'akshare_bundle_size', label: 'AKShare 标的数', kind: 'number' },
  { key: 'akshare_history_sessions', label: 'AKShare 历史交易日', kind: 'number' },
  { key: 'akshare_fetch_max_attempts', label: 'AKShare 最大尝试次数', kind: 'number' },
  { key: 'akshare_fetch_backoff_seconds', label: 'AKShare 重试退避（秒）', kind: 'number' },
  { key: 'minimum_listing_days', label: '最低上市天数', kind: 'number' },
  { key: 'minimum_median_amount', label: '最低成交额', kind: 'number' },
  { key: 'allow_demo_data', label: '允许演示数据', kind: 'checkbox' },
]

const bytes = (value?: number | null) => {
  if (value === undefined || value === null) return '—'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let amount = value
  let unit = 0
  while (Math.abs(amount) >= 1024 && unit < units.length - 1) { amount /= 1024; unit += 1 }
  return `${amount.toFixed(unit < 2 ? 0 : 1)} ${units[unit]}`
}

const RESOURCE_WARNING_TEXT: Record<string, string> = {
  MEMORY_USAGE_HIGH: '运行环境内存使用率较高，请重点关注研究任务启动后的余量。',
  DUAL_CEILING_EXCEEDS_AVAILABLE: '当前可用内存低于两个研究 Worker 的最大预算；可继续启用，但高峰期可能触发内存压力。',
  DUAL_PROJECTED_HEADROOM_LOW: '按当前实测基线估算，启用 DUAL 后的内存余量偏低。',
  CPU_USAGE_HIGH: 'CPU 使用率较高，任务吞吐可能暂时下降。',
  DISK_USAGE_HIGH: '运行磁盘空间偏紧，请检查数据湖、对象和日志保留空间。',
}

function sourceText(settings: SystemSettings | null, field: string) {
  return settings?.sources[field] === 'database' ? '已保存覆盖' : '环境变量'
}

export function SystemSettingsPage() {
  const { user } = useAuth()
  const isAdmin = user?.role?.toLowerCase() === 'admin'
  const [current, setCurrent] = useState<SystemSettings | null>(null)
  const [resources, setResources] = useState<SystemResources | null>(null)
  const [resourceLevel, setResourceLevel] = useState<'NORMAL' | 'WARNING' | 'CRITICAL'>('NORMAL')
  const warningSamples = useRef(0)
  const [form, setForm] = useState<SystemSettingsDraft>({})
  const [secrets, setSecrets] = useState<Record<string, string>>({})
  const [unlockToken, setUnlockToken] = useState('')
  const [unlockExpiresAt, setUnlockExpiresAt] = useState('')
  const [unlockOpen, setUnlockOpen] = useState(false)
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [resourceError, setResourceError] = useState('')
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [clock, setClock] = useState(Date.now())
  const unlocked = Boolean(unlockToken) && new Date(unlockExpiresAt).getTime() > clock

  const loadSettings = useCallback(async () => {
    const settings = await api.systemSettings()
    setCurrent(settings)
    setForm({ ...settings.values })
  }, [])

  const loadResources = useCallback(async () => {
    try {
      const next = await api.systemResources()
      setResources(next)
      setResourceError('')
      const level = next.level
      if (level === 'CRITICAL') {
        warningSamples.current = 0
        setResourceLevel('CRITICAL')
      } else if (level === 'WARNING') {
        warningSamples.current += 1
        if (warningSamples.current >= 3) setResourceLevel('WARNING')
      } else {
        warningSamples.current = 0
        setResourceLevel('NORMAL')
      }
    } catch (reason) {
      setResourceError(reason instanceof Error ? reason.message : '系统资源加载失败')
    }
  }, [])

  useEffect(() => {
    if (!isAdmin) return
    void loadSettings()
      .catch((reason) => setError(reason instanceof Error ? reason.message : '系统设置加载失败'))
      .finally(() => setLoading(false))
  }, [isAdmin, loadSettings])

  useEffect(() => {
    if (!isAdmin) return
    let poll: number | undefined
    const start = () => {
      if (poll !== undefined || document.visibilityState !== 'visible') return
      void loadResources()
      poll = window.setInterval(() => void loadResources(), 5000)
    }
    const stop = () => { if (poll !== undefined) window.clearInterval(poll); poll = undefined }
    const visibility = () => { if (document.visibilityState === 'visible') start(); else stop() }
    document.addEventListener('visibilitychange', visibility)
    start()
    return () => { stop(); document.removeEventListener('visibilitychange', visibility) }
  }, [isAdmin, loadResources])

  useEffect(() => {
    const timer = window.setInterval(() => setClock(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [])

  useEffect(() => {
    if (!unlockToken) return
    const remaining = new Date(unlockExpiresAt).getTime() - Date.now()
    const expire = () => { setUnlockToken(''); setUnlockExpiresAt(''); setSecrets({}); setMessage('设置编辑权限已到期') }
    if (remaining <= 0) { expire(); return }
    const timer = window.setTimeout(expire, remaining)
    return () => window.clearTimeout(timer)
  }, [unlockExpiresAt, unlockToken])

  if (!isAdmin) return <Navigate to="/" replace />

  function lock() {
    setUnlockToken('')
    setUnlockExpiresAt('')
    setSecrets({})
    setPassword('')
    setShowPassword(false)
  }

  function closeUnlock() {
    setUnlockOpen(false)
    setPassword('')
    setShowPassword(false)
  }

  async function unlock(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError(''); setMessage('')
    try {
      const result = await api.unlockSystemSettings(password)
      setUnlockToken(result.unlock_token)
      setUnlockExpiresAt(result.expires_at)
      setPassword('')
      closeUnlock()
      setMessage('系统设置已解锁')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '身份验证失败')
    } finally { setBusy(false) }
  }

  function update(key: string, value: string | number | boolean | null) {
    if (!unlocked) return
    setForm((previous) => ({ ...previous, [key]: value }))
  }

  function changedPayload(): SystemSettingsDraft {
    const values: SystemSettingsDraft = {}
    for (const [key, value] of Object.entries(form)) {
      if (String(value ?? '') !== String(current?.values[key] ?? '')) values[key] = value
    }
    for (const [key, value] of Object.entries(secrets)) if (value.trim()) values[key] = value.trim()
    return values
  }

  async function save(event: FormEvent) {
    event.preventDefault()
    if (!unlocked) { setUnlockOpen(true); return }
    setBusy(true); setError(''); setMessage('')
    try {
      const payload = changedPayload()
      if (!Object.keys(payload).length) { setMessage('没有需要保存的修改'); return }
      const settings = await api.saveSystemSettings(payload, unlockToken)
      setCurrent(settings); setForm({ ...settings.values }); setSecrets({})
      setMessage(`系统配置 v${settings.version} 已保存。${settings.restart_required ? '执行拓扑待 Docker 重启后生效。' : '设置将在下一次请求或任务启动时生效。'}`)
    } catch (reason) {
      if (reason instanceof ApiError && reason.code === 'SYSTEM_SETTINGS_LOCKED') {
        lock(); setUnlockOpen(true)
      }
      setError(reason instanceof Error ? reason.message : '保存系统设置失败')
    } finally { setBusy(false) }
  }

  async function restore(field?: string) {
    if (!unlocked) { setUnlockOpen(true); return }
    setBusy(true); setError(''); setMessage('')
    try {
      const settings = field ? await api.restoreSystemSetting(field, unlockToken) : await api.restoreAllSystemSettings(unlockToken)
      setCurrent(settings); setForm({ ...settings.values }); setSecrets({})
      setMessage(field ? `${field} 已恢复为环境变量值` : '所有界面覆盖项已恢复为环境变量值')
    } catch (reason) {
      if (reason instanceof ApiError && reason.code === 'SYSTEM_SETTINGS_LOCKED') {
        lock(); setUnlockOpen(true)
      }
      setError(reason instanceof Error ? reason.message : '恢复失败')
    } finally { setBusy(false) }
  }

  function renderFields(fields: Field[]) {
    return fields.map((field) => <label key={field.key} className={field.kind === 'checkbox' ? 'system-checkbox' : ''}>{field.label}
      {field.kind === 'checkbox'
        ? <input disabled={!unlocked || busy} type="checkbox" checked={Boolean(form[field.key])} onChange={(event) => update(field.key, event.target.checked)} />
        : <input disabled={!unlocked || busy} type={field.kind} value={String(form[field.key] ?? '')} onChange={(event) => update(field.key, field.kind === 'number' ? Number(event.target.value) : event.target.value)} />}
      <small className="form-hint">{field.hint || sourceText(current, field.key)}</small>
    </label>)
  }

  const stale = !resources || clock - new Date(resources.collected_at).getTime() > 15_000
  const topology = resources?.topology_estimate

  return <div className="system-settings-page">
    <section className="system-settings-toolbar">
      <div><span className="eyebrow">ADMIN CONTROL</span><h2>系统设置</h2><p>{unlocked ? `编辑权限至 ${formatTime(unlockExpiresAt)}` : '监控可用，配置写入已锁定'}</p></div>
      <div className="row-actions">
        <button type="button" className="secondary icon-button" title="刷新资源" onClick={() => void loadResources()}><RefreshCw size={16} /></button>
        {unlocked
          ? <button type="button" className="secondary" onClick={lock}><LockKeyhole size={15} />锁定</button>
          : <button type="button" className="primary" onClick={() => { setError(''); setUnlockOpen(true) }}><UnlockKeyhole size={15} />解锁设置</button>}
      </div>
    </section>

    <Panel title="系统资源" eyebrow={resources?.scope_label || 'RESOURCE MONITOR'} action={<span className={`resource-state ${stale ? 'stale' : resourceLevel.toLowerCase()}`}>{stale ? '数据陈旧' : resourceLevel}</span>}>
      {loading && !resources ? <Loading /> : <>
        <div className="resource-metrics">
          <article><MemoryStick size={18} /><span>内存</span><strong>{resources?.memory.percent.toFixed(1) ?? '—'}%</strong><div className="resource-progress" aria-label="内存使用率"><i style={{ width: `${Math.min(100, resources?.memory.percent || 0)}%` }} /></div><small>{bytes(resources?.memory.used_bytes)} / {bytes(resources?.memory.total_bytes)}</small></article>
          <article><Cpu size={18} /><span>CPU</span><strong>{resources?.cpu.percent.toFixed(1) ?? '—'}%</strong><small>{resources?.cpu.logical_cores ?? '—'} 个逻辑核心</small></article>
          <article><HardDrive size={18} /><span>磁盘</span><strong>{resources?.disk.percent.toFixed(1) ?? '—'}%</strong><small>{bytes(resources?.disk.available_bytes)} 可用</small></article>
          <article><Server size={18} /><span>DUAL · {topology?.worker_replicas ?? '—'} Worker</span><strong>{bytes(topology?.typical_increment_bytes)}</strong><small>典型新增 · 上限 {bytes(topology?.maximum_increment_bytes)}</small></article>
        </div>
        {resources?.warnings.length ? <div className={`resource-advisory ${resourceLevel.toLowerCase()}`}>{resources.warnings.map((item) => <p key={item}>{RESOURCE_WARNING_TEXT[item] || item}</p>)}</div> : null}
        <div className="resource-table desktop-only"><table><thead><tr><th>服务</th><th>角色</th><th>状态</th><th>内存</th><th>上限</th><th>CPU</th><th>采集时间</th></tr></thead><tbody>{resources?.services.map((service) => <tr key={service.service_id}><td><strong>{service.service_id}</strong></td><td>{service.role}</td><td><StatusPill status={service.healthy ? 'ACTIVE' : 'ERROR'} /></td><td>{bytes(service.memory_used_bytes)}</td><td>{bytes(service.memory_limit_bytes)}</td><td>{service.cpu_percent == null ? '—' : `${service.cpu_percent.toFixed(1)}%`}</td><td>{formatTime(service.collected_at || resources.collected_at)}</td></tr>)}</tbody></table></div>
        <div className="resource-service-list mobile-only">{resources?.services.map((service) => <article key={service.service_id}><header><div><strong>{service.service_id}</strong><small>{service.role}</small></div><StatusPill status={service.healthy ? 'ACTIVE' : 'ERROR'} /></header><dl><div><dt>内存</dt><dd>{bytes(service.memory_used_bytes)}</dd></div><div><dt>CPU</dt><dd>{service.cpu_percent == null ? '—' : `${service.cpu_percent.toFixed(1)}%`}</dd></div></dl></article>)}</div>
        <p className="resource-collected">采集于 {formatTime(resources?.collected_at)} · {resources?.scope || '—'}</p>
      </>}
      <ErrorNotice message={resourceError} />
    </Panel>

    <div className={`system-settings-lock-note ${unlocked ? 'unlocked' : ''}`}><ShieldCheck size={17} /><span>{unlocked ? '已通过当前管理员密码验证，保存与恢复操作已启用。' : '只读监控模式。解锁后才能修改、保存或恢复系统设置。'}</span></div>

    <div className="system-settings-grid">
      <Panel title="执行模式" eyebrow="WORKER TOPOLOGY" action={<StatusPill status={current?.restart_required ? 'PENDING RESTART' : current?.actual_loaded_mode === 'DUAL' ? 'DUAL ACTIVE' : 'SERIAL ACTIVE'} />}>
        {loading ? <Loading /> : <form className="run-form" onSubmit={save}>
          <label>研究执行模式<select disabled={!unlocked || busy} value={String(form.research_execution_mode || 'SERIAL')} onChange={(event) => update('research_execution_mode', event.target.value)}><option value="SERIAL">SERIAL：job-worker 串行处理全部任务</option><option value="DUAL">DUAL：两个 research-worker 并行处理研究</option></select></label>
          <label>每项研究 LLM Agent 并发（1–4）<input disabled={!unlocked || busy} type="number" min={1} max={4} value={Number(form.llm_agent_max_concurrency || 4)} onChange={(event) => update('llm_agent_max_concurrency', Number(event.target.value))} /></label>
          <div className="search-result-meta"><div><span>已保存模式</span><strong>{String(current?.values.research_execution_mode || 'SERIAL')}</strong></div><div><span>实际加载模式</span><strong>{current?.actual_loaded_mode || 'UNKNOWN'}</strong></div><div><span>双模式 LLM 上限</span><strong>{2 * Number(form.llm_agent_max_concurrency || 4)}</strong></div><div><span>网关容量</span><strong>{String(current?.read_only_environment.model_gateway_max_concurrency ?? '—')}</strong></div></div>
          {current?.restart_required && <div className="snapshot-isolation">配置已保存，待 Docker 重启生效。<code>{current.compose_restart_command}</code></div>}
          <button className="primary" disabled={!unlocked || busy}>{busy ? '保存中…' : '保存执行设置'}</button>
        </form>}
      </Panel>

      <Panel title="Worker 与队列" eyebrow="RUNTIME OBSERVABILITY">
        {!current ? <Loading /> : <div className="runtime-stack"><div className="table-wrap desktop-only"><table><thead><tr><th>Worker</th><th>加载模式</th><th>心跳</th><th>拓扑</th></tr></thead><tbody>{current.workers.length ? current.workers.map((worker) => <tr key={worker.worker_id}><td><strong>{worker.role}</strong><small>{worker.worker_id}</small></td><td>{worker.loaded_mode}</td><td><StatusPill status={worker.healthy ? 'ACTIVE' : 'ERROR'} /></td><td>{worker.topology_sha256 === current.topology_sha256 ? '当前配置' : '等待重启'}</td></tr>) : <tr><td colSpan={4}>尚未收到 Worker 心跳</td></tr>}</tbody></table></div><div className="worker-mobile-list mobile-only">{current.workers.length ? current.workers.map((worker) => <article key={worker.worker_id}><div><strong>{worker.role}</strong><small>{worker.worker_id}</small></div><StatusPill status={worker.healthy ? 'ACTIVE' : 'ERROR'} /><span>{worker.loaded_mode} · {worker.topology_sha256 === current.topology_sha256 ? '当前配置' : '等待重启'}</span></article>) : <p className="form-hint">尚未收到 Worker 心跳</p>}</div><div className="queue-list">{Object.entries(current.queues).map(([name, queue]) => <div key={name}><strong>{name}</strong><span>待处理 {queue.pending}</span><span>处理中 {queue.processing}</span></div>)}</div></div>}
      </Panel>
    </div>

    <Panel title="高级配置" eyebrow="VERSIONED OVERRIDES" className="advanced-settings">
      <form className="run-form" onSubmit={save}>
        <details open><summary><span>数据存储与凭据</span><small>对象存储、数据源密钥</small></summary><div className="advanced-fields">{renderFields(STORAGE_FIELDS)}<label>Tushare Token<input disabled={!unlocked || busy} type="password" value={secrets.tushare_token || ''} placeholder={current?.secret_configured.tushare_token ? '已配置；留空不修改' : '未配置'} onChange={(event) => setSecrets((previous) => ({ ...previous, tushare_token: event.target.value }))} /></label><label>对象存储 Access Key<input disabled={!unlocked || busy} type="password" value={secrets.object_store_access_key || ''} placeholder={current?.secret_configured.object_store_access_key ? '已配置；留空不修改' : '未配置'} onChange={(event) => setSecrets((previous) => ({ ...previous, object_store_access_key: event.target.value }))} /></label><label>对象存储 Secret Key<input disabled={!unlocked || busy} type="password" value={secrets.object_store_secret_key || ''} placeholder={current?.secret_configured.object_store_secret_key ? '已配置；留空不修改' : '未配置'} onChange={(event) => setSecrets((previous) => ({ ...previous, object_store_secret_key: event.target.value }))} /></label><button type="button" className="secondary" disabled={!unlocked || busy} onClick={() => void restore('object_store_endpoint')}>恢复 Endpoint 环境值</button></div></details>
        <details><summary><span>搜索与行情</span><small>缓存、并发、超时</small></summary><div className="advanced-fields">{renderFields(SEARCH_MARKET_FIELDS)}</div></details>
        <details><summary><span>研究采集</span><small>计划、供应商、数据门槛</small></summary><div className="advanced-fields">{renderFields(RESEARCH_FIELDS)}<label>数据包模式<select disabled={!unlocked || busy} value={String(form.canonical_bundle_mode || 'akshare')} onChange={(event) => update('canonical_bundle_mode', event.target.value)}><option value="akshare">AKShare</option><option value="file">文件</option><option value="demo">演示数据</option></select></label></div></details>
        <footer className="advanced-actions"><div><Link to="/admin/models">前往模型设置</Link><small>部署基线仍由环境变量与 Compose 管理</small></div><div className="row-actions"><button type="button" className="danger-button" disabled={!unlocked || busy} onClick={() => { if (window.confirm('恢复全部系统设置为环境变量值？')) void restore() }}>恢复全部覆盖</button><button className="primary" disabled={!unlocked || busy}>{busy ? '保存中…' : '保存高级配置'}</button></div></footer>
      </form>
      <ErrorNotice message={error} />
      {message && <div className="snapshot-isolation">{message}</div>}
    </Panel>

    {unlockOpen && <div className="automatic-settings-overlay" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) closeUnlock() }}><form className="system-unlock-dialog" role="dialog" aria-modal="true" aria-labelledby="system-unlock-title" onSubmit={unlock}><header><div><span className="eyebrow">SECURITY CHECK</span><h2 id="system-unlock-title">解锁系统设置</h2></div><button type="button" className="secondary icon-button" title="关闭" disabled={busy} onClick={closeUnlock}><X size={17} /></button></header><p>管理员 <strong>{user?.username}</strong></p><label>当前账户密码<div className="password-control"><input autoFocus required autoComplete="current-password" type={showPassword ? 'text' : 'password'} value={password} onChange={(event) => setPassword(event.target.value)} /><button type="button" className="secondary icon-button" title={showPassword ? '隐藏密码' : '显示密码'} onClick={() => setShowPassword((value) => !value)}>{showPassword ? <EyeOff size={16} /> : <Eye size={16} />}</button></div></label><ErrorNotice message={error} /><footer><button type="button" className="secondary" disabled={busy} onClick={closeUnlock}>取消</button><button className="primary" disabled={busy || !password}>{busy ? '验证中…' : '验证并解锁'}</button></footer></form></div>}
  </div>
}
