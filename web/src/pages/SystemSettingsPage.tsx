import { useEffect, useState, type FormEvent } from 'react'
import { Link, Navigate } from 'react-router-dom'
import { api } from '../api'
import { Empty, ErrorNotice, formatTime, Loading, Panel, StatusPill } from '../components/Ui'
import { useAuth } from '../context/AuthContext'
import type { SystemSettings, SystemSettingsDraft } from '../types'

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

function sourceText(settings: SystemSettings | null, field: string) {
  return settings?.sources[field] === 'database' ? '已保存覆盖' : '环境变量'
}

export function SystemSettingsPage() {
  const { user } = useAuth()
  const isAdmin = user?.role?.toLowerCase() === 'admin'
  const [current, setCurrent] = useState<SystemSettings | null>(null)
  const [form, setForm] = useState<SystemSettingsDraft>({})
  const [secrets, setSecrets] = useState<Record<string, string>>({})
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')

  const load = async () => {
    const settings = await api.systemSettings()
    setCurrent(settings)
    setForm({ ...settings.values })
  }

  useEffect(() => {
    if (!isAdmin) return
    void load().catch((reason) => setError(reason instanceof Error ? reason.message : '系统设置加载失败')).finally(() => setLoading(false))
  }, [isAdmin])

  if (!isAdmin) return <Navigate to="/" replace />

  function update(key: string, value: string | number | boolean | null) {
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
    event.preventDefault(); setBusy(true); setError(''); setMessage('')
    try {
      const payload = changedPayload()
      if (!Object.keys(payload).length) { setMessage('没有需要保存的修改'); return }
      const settings = await api.saveSystemSettings(payload)
      setCurrent(settings); setForm({ ...settings.values }); setSecrets({})
      setMessage(`系统配置 v${settings.version} 已保存。${settings.restart_required ? '执行拓扑待 Docker 重启后生效。' : '非拓扑设置将在下一次请求或任务启动时生效。'}`)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存系统设置失败')
    } finally { setBusy(false) }
  }

  async function restore(field?: string) {
    setBusy(true); setError(''); setMessage('')
    try {
      const settings = field ? await api.restoreSystemSetting(field) : await api.restoreAllSystemSettings()
      setCurrent(settings); setForm({ ...settings.values }); setSecrets({})
      setMessage(field ? `${field} 已恢复为环境变量值` : '所有界面覆盖项已恢复为环境变量值')
    } catch (reason) { setError(reason instanceof Error ? reason.message : '恢复失败') } finally { setBusy(false) }
  }

  function renderFields(fields: Field[]) {
    return fields.map((field) => <label key={field.key}>{field.label}
      {field.kind === 'checkbox'
        ? <input type="checkbox" checked={Boolean(form[field.key])} onChange={(event) => update(field.key, event.target.checked)} />
        : <input type={field.kind} value={String(form[field.key] ?? '')} onChange={(event) => update(field.key, field.kind === 'number' ? Number(event.target.value) : event.target.value)} />}
      <small className="form-hint">{field.hint || sourceText(current, field.key)}</small>
    </label>)
  }

  return <div className="admin-layout">
    <Panel title="执行模式" eyebrow="WORKER TOPOLOGY" action={<StatusPill status={current?.restart_required ? 'PENDING RESTART' : current?.actual_loaded_mode === 'DUAL' ? 'DUAL ACTIVE' : 'SERIAL ACTIVE'} />}>
      {loading ? <Loading /> : <form className="run-form" onSubmit={save}>
        <label>研究执行模式<select value={String(form.research_execution_mode || 'SERIAL')} onChange={(event) => update('research_execution_mode', event.target.value)}>
          <option value="SERIAL">SERIAL：job-worker 串行处理全部任务</option>
          <option value="DUAL">DUAL：两个 research-worker 并行处理研究</option>
        </select></label>
        <label>每项研究 LLM Agent 并发（1–4）<input type="number" min={1} max={4} value={Number(form.llm_agent_max_concurrency || 4)} onChange={(event) => update('llm_agent_max_concurrency', Number(event.target.value))} /></label>
        <div className="search-result-meta">
          <div><span>已保存模式</span><strong>{String(current?.values.research_execution_mode || 'SERIAL')}</strong></div>
          <div><span>实际加载模式</span><strong>{current?.actual_loaded_mode || 'UNKNOWN'}</strong></div>
          <div><span>双模式总 LLM 上限</span><strong>{2 * Number(form.llm_agent_max_concurrency || 4)}</strong></div>
          <div><span>网关容量（环境只读）</span><strong>{String(current?.read_only_environment.model_gateway_max_concurrency ?? '—')}</strong></div>
        </div>
        <p className="form-hint">默认 SERIAL 不启动 research-worker；保存 DUAL 后按下方命令启用 dual-research profile 并启动两个副本。双模式需要至少 4GB 可用内存及足够模型网关容量；活动研究或回测存在时，系统会拒绝切换。队列优先级固定，不能在此编辑。</p>
        {current?.restart_required && <div className="snapshot-isolation">配置已保存，待 Docker 重启生效。<code>{current.compose_restart_command}</code></div>}
        <div className="row-actions"><button className="primary" disabled={busy}>{busy ? '保存中…' : '保存系统设置'}</button></div>
      </form>}
    </Panel>

    <Panel title="Worker 与队列状态" eyebrow="RUNTIME OBSERVABILITY">
      {!current ? <Loading /> : <>
        <div className="table-wrap"><table><thead><tr><th>Worker</th><th>加载模式</th><th>心跳</th><th>拓扑</th></tr></thead><tbody>{current.workers.length ? current.workers.map((worker) => <tr key={worker.worker_id}><td><strong>{worker.role}</strong><small>{worker.worker_id}</small></td><td>{worker.loaded_mode}</td><td><StatusPill status={worker.healthy ? 'HEALTHY' : 'OFFLINE'} /></td><td>{worker.topology_sha256 === current.topology_sha256 ? '已加载当前配置' : '等待重启'}</td></tr>) : <tr><td colSpan={4}>尚未收到 Worker 心跳</td></tr>}</tbody></table></div>
        <div className="table-wrap"><table><thead><tr><th>队列</th><th>待处理</th><th>处理中</th></tr></thead><tbody>{Object.entries(current.queues).map(([name, queue]) => <tr key={name}><td>{name}</td><td>{queue.pending}</td><td>{queue.processing}</td></tr>)}</tbody></table></div>
      </>}
    </Panel>

    <Panel title="数据存储与凭据" eyebrow="VERSIONED OVERRIDES">
      <form className="run-form" onSubmit={save}>
        {renderFields(STORAGE_FIELDS)}
        <label>Tushare Token<input type="password" value={secrets.tushare_token || ''} placeholder={current?.secret_configured.tushare_token ? '已配置；留空不修改' : '未配置'} onChange={(event) => setSecrets({ ...secrets, tushare_token: event.target.value })} /></label>
        <label>对象存储 Access Key<input type="password" value={secrets.object_store_access_key || ''} placeholder={current?.secret_configured.object_store_access_key ? '已配置；留空不修改' : '未配置'} onChange={(event) => setSecrets({ ...secrets, object_store_access_key: event.target.value })} /></label>
        <label>对象存储 Secret Key<input type="password" value={secrets.object_store_secret_key || ''} placeholder={current?.secret_configured.object_store_secret_key ? '已配置；留空不修改' : '未配置'} onChange={(event) => setSecrets({ ...secrets, object_store_secret_key: event.target.value })} /></label>
        <div className="row-actions"><button className="primary" disabled={busy}>{busy ? '保存中…' : '保存修改'}</button><button type="button" className="secondary" disabled={busy} onClick={() => void restore('object_store_endpoint')}>恢复 Endpoint 环境值</button></div>
      </form>
    </Panel>

    <Panel title="搜索与行情" eyebrow="HOT RELOAD">
      <form className="run-form" onSubmit={save}>{renderFields(SEARCH_MARKET_FIELDS)}<button className="primary" disabled={busy}>{busy ? '保存中…' : '保存修改'}</button></form>
    </Panel>

    <Panel title="研究采集" eyebrow="SCHEDULE AND PROVIDERS">
      <form className="run-form" onSubmit={save}>
        {renderFields(RESEARCH_FIELDS)}
        <label>数据包模式<select value={String(form.canonical_bundle_mode || 'akshare')} onChange={(event) => update('canonical_bundle_mode', event.target.value)}><option value="akshare">AKShare</option><option value="file">文件</option><option value="demo">演示数据</option></select></label>
        <button className="primary" disabled={busy}>{busy ? '保存中…' : '保存修改'}</button>
      </form>
    </Panel>

    <Panel title="环境基线与模型设置" eyebrow="DEPLOYMENT-ONLY">
      <p className="form-hint">数据库/Redis 地址、认证、端口、卷路径、Fernet 密钥、策略路径和 Docker 资源限制仅由环境变量与 Compose 管理。模型 API 凭据保持独立。</p>
      <p><Link to="/admin/models">前往模型设置</Link></p>
      <div className="row-actions"><button className="danger-button" disabled={busy} onClick={() => { if (window.confirm('恢复全部系统设置为环境变量值？')) void restore() }}>恢复全部环境变量覆盖</button></div>
      <ErrorNotice message={error} />
      {message && <div className="snapshot-isolation">{message}</div>}
    </Panel>
  </div>
}
