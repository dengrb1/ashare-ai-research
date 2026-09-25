import { motion, useReducedMotion } from 'framer-motion'
import { Ban, Brain, CheckCircle2, Database, Download, KeyRound, Play, RefreshCw, RotateCcw, ShieldCheck, Trash2, Upload, UserCog, UserPlus, Wrench } from 'lucide-react'
import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { api, downloadPersonalArchive } from '../api'
import { useAuth } from '../context/AuthContext'
import type { ModelSettings, PersonalArchiveJob, SystemSettings, TrainingHistory, TrainingStatus, User } from '../types'
import { Empty, ErrorNotice, Loading, Panel, StatusPill, formatTime } from '../components/Ui'

type SettingsTab = 'system' | 'model' | 'jev' | 'account' | 'data'
type JevModel = { version: string; path: string; mode: string }

const activeJob = (job: PersonalArchiveJob | null) => Boolean(job && ['PENDING', 'PROCESSING'].includes(job.status))

function archivePreview(job: PersonalArchiveJob | null) {
  return (job?.result || {}) as {
    watchlist?: { new?: string[]; duplicate?: string[] }
    positions?: { conflicts?: Array<{ symbol: string; current: unknown; imported: unknown }> }
    total_assets?: { current?: string | null; imported?: string | null }
    history?: Record<string, number>
  }
}

export function SettingsPage() {
  const reduceMotion = useReducedMotion()
  const { user } = useAuth()
  const admin = user?.role?.toLowerCase() === 'admin'
  const [tab, setTab] = useState<SettingsTab>(admin ? 'system' : 'data')
  const [system, setSystem] = useState<SystemSettings | null>(null)
  const [model, setModel] = useState<ModelSettings | null>(null)
  const [users, setUsers] = useState<User[]>([])
  const [jevModels, setJevModels] = useState<JevModel[]>([])
  const [training, setTraining] = useState<TrainingStatus | null>(null)
  const [trainingHistory, setTrainingHistory] = useState<TrainingHistory | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [unlockPassword, setUnlockPassword] = useState('')
  const [unlockToken, setUnlockToken] = useState('')
  const [jevApiKey, setJevApiKey] = useState('')
  const [archivePassphrase, setArchivePassphrase] = useState('')
  const [importPassphrase, setImportPassphrase] = useState('')
  const [importFile, setImportFile] = useState<File | null>(null)
  const [exportJob, setExportJob] = useState<PersonalArchiveJob | null>(null)
  const [importJob, setImportJob] = useState<PersonalArchiveJob | null>(null)
  const [applyJob, setApplyJob] = useState<PersonalArchiveJob | null>(null)
  const [mergeOptions, setMergeOptions] = useState<Record<string, unknown>>({})
  const [newUser, setNewUser] = useState({ username: '', password: '', role: 'USER' })
  const [draft, setDraft] = useState({ research_model: 'gpt-5.6-sol', research_reasoning_effort: 'high', base_url: '', api_key: '', timeout_seconds: 90, enabled: true, model_profiles: [] as ModelSettings['model_profiles'] })

  const loadAdmin = async () => {
    if (!admin) return
    const [systemSettings, modelSettings, accountUsers, models, currentTraining, history] = await Promise.all([
      api.systemSettings(), api.modelSettings(), api.users(), api.decisionModels(), api.currentJevTraining(), api.jevTrainingHistory(),
    ])
    setSystem(systemSettings); setModel(modelSettings); setUsers(accountUsers); setJevModels(models.models || []); setTraining(currentTraining); setTrainingHistory(history)
    setDraft((current) => ({ ...current, research_model: modelSettings.research_model, research_reasoning_effort: modelSettings.research_reasoning_effort, base_url: modelSettings.base_url, timeout_seconds: modelSettings.timeout_seconds, enabled: modelSettings.enabled, model_profiles: modelSettings.model_profiles }))
  }

  const load = async () => {
    setLoading(true); setError('')
    try { await loadAdmin() } catch (reason) { setError(reason instanceof Error ? reason.message : '设置加载失败') } finally { setLoading(false) }
  }
  useEffect(() => { void load() }, [admin])

  useEffect(() => {
    if (!admin || !training || !['queued', 'running'].includes(training.status)) return
    const timer = window.setInterval(() => void api.currentJevTraining().then(setTraining).catch(() => undefined), 3_000)
    return () => window.clearInterval(timer)
  }, [admin, training?.status])

  useEffect(() => {
    if (!activeJob(exportJob)) return
    const timer = window.setInterval(() => void api.personalExport(exportJob!.archive_id).then(setExportJob).catch(() => undefined), 1_500)
    return () => window.clearInterval(timer)
  }, [exportJob?.archive_id, exportJob?.status])

  useEffect(() => {
    if (!activeJob(importJob) && !activeJob(applyJob)) return
    const id = (activeJob(importJob) ? importJob : applyJob)!.archive_id
    const loadStatus = () => void api.personalImport(id).then((next) => {
      if (importJob?.archive_id === id) setImportJob(next)
      if (applyJob?.archive_id === id) setApplyJob(next)
    }).catch(() => undefined)
    const timer = window.setInterval(loadStatus, 1_500)
    return () => window.clearInterval(timer)
  }, [applyJob?.archive_id, applyJob?.status, importJob?.archive_id, importJob?.status])

  async function saveModel(event: FormEvent) {
    event.preventDefault(); setBusy(true); setMessage(null)
    try { setModel(await api.saveModelSettings(draft)); setMessage('模型设置已保存') } catch (reason) { setError(reason instanceof Error ? reason.message : '模型设置保存失败') } finally { setBusy(false) }
  }

  async function saveSystem(event: FormEvent) {
    event.preventDefault(); if (!system) return
    setBusy(true); setMessage(null)
    try {
      let token = unlockToken
      if (!token) {
        if (!unlockPassword) throw new Error('请输入管理员密码以解锁设置')
        token = (await api.unlockSystemSettings(unlockPassword)).unlock_token
        setUnlockToken(token); setUnlockPassword('')
      }
      const payload: Record<string, string | number | boolean> = {
        jev_confidence_threshold: Number(system.values.jev_confidence_threshold ?? 0.6),
        decision_system2_enabled: Boolean(system.values.decision_system2_enabled ?? true),
        jev_backend: String(system.values.jev_backend ?? 'local'),
        jev_live_base_url: String(system.values.jev_live_base_url ?? ''),
        jev_live_model: String(system.values.jev_live_model ?? 'jev-live'),
        jev_live_endpoint: String(system.values.jev_live_endpoint ?? '/v1/decisions'),
        jev_live_timeout_seconds: Number(system.values.jev_live_timeout_seconds ?? 30),
      }
      if (jevApiKey.trim()) payload.jev_live_api_key = jevApiKey.trim()
      setSystem(await api.saveSystemSettings(payload, token)); setJevApiKey('')
      setMessage('系统设置已保存')
    } catch (reason) { setError(reason instanceof Error ? reason.message : '系统设置保存失败') } finally { setBusy(false) }
  }

  async function createUser(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError('')
    try { await api.createUser(newUser); setNewUser({ username: '', password: '', role: 'USER' }); setUsers(await api.users()); setMessage('用户已创建') } catch (reason) { setError(reason instanceof Error ? reason.message : '创建用户失败') } finally { setBusy(false) }
  }

  async function changeUser(account: User, action: 'toggle' | 'reset' | 'delete') {
    const id = account.id || account.user_id || account.username
    try {
      if (action === 'toggle') await api.setUserDisabled(id, !(account.disabled || account.enabled === false || account.is_active === false))
      if (action === 'reset') { const password = window.prompt(`为 ${account.username} 设置新密码（至少 10 位）`); if (!password) return; await api.resetPassword(id, password) }
      if (action === 'delete') { if (!window.confirm(`确定删除用户 ${account.username}？`)) return; await api.deleteUser(id) }
      setUsers(await api.users()); setMessage('账户状态已更新')
    } catch (reason) { setError(reason instanceof Error ? reason.message : '账户操作失败') }
  }

  async function startExport() {
    if (archivePassphrase.length < 8) return setError('导出密码短语至少需要 8 个字符')
    setBusy(true); setError('')
    try { setExportJob(await api.createPersonalExport(archivePassphrase)); setArchivePassphrase(''); setMessage('导出任务已提交') } catch (reason) { setError(reason instanceof Error ? reason.message : '导出任务提交失败') } finally { setBusy(false) }
  }

  async function downloadExport() {
    if (!exportJob) return
    setBusy(true); setError('')
    try { const blob = await downloadPersonalArchive(exportJob.archive_id); const url = URL.createObjectURL(blob); const link = document.createElement('a'); link.href = url; link.download = 'personal-profile.ashare'; link.click(); window.setTimeout(() => URL.revokeObjectURL(url), 1_000) } catch (reason) { setError(reason instanceof Error ? reason.message : '档案下载失败') } finally { setBusy(false) }
  }

  async function startImport() {
    if (!importFile || importPassphrase.length < 8) return setError('请选择档案文件并输入至少 8 个字符的口令')
    setBusy(true); setError(''); setApplyJob(null); setMergeOptions({})
    try { setImportJob(await api.uploadPersonalImport(importFile, importPassphrase)); setImportPassphrase(''); setMessage('导入预览任务已提交') } catch (reason) { setError(reason instanceof Error ? reason.message : '导入任务提交失败') } finally { setBusy(false) }
  }

  async function applyImport() {
    if (!importJob) return
    setBusy(true); setError('')
    try { setApplyJob(await api.applyPersonalImport(importJob.archive_id, mergeOptions, crypto.randomUUID())); setMessage('合并任务已提交') } catch (reason) { setError(reason instanceof Error ? reason.message : '合并任务提交失败') } finally { setBusy(false) }
  }

  async function triggerTraining() {
    setBusy(true); setError('')
    try { const queued = await api.triggerJevTraining(); setTraining(await api.jevTrainingStatus(queued.training_id)); setMessage('Jev 训练已进入 job-worker 队列') } catch (reason) { setError(reason instanceof Error ? reason.message : '训练任务提交失败') } finally { setBusy(false) }
  }

  const preview = useMemo(() => archivePreview(importJob), [importJob])
  const conflicts = preview.positions?.conflicts || []
  const tabs: Array<[SettingsTab, string, typeof Wrench]> = [['system', '系统与运行时', Wrench], ['model', '模型与密钥', KeyRound], ['jev', 'Jev 模型', Brain], ['account', '用户与权限', UserCog], ['data', '个人数据', ShieldCheck]]

  return <div className="page-stack settings-page">
    <section className="settings-head"><div><div className="eyebrow">CONTROL PLANE</div><h2>设置</h2><p>运行时、模型、Jev 训练和个人数据统一从这里管理。</p></div><button className="secondary icon-button" title="重新加载" onClick={() => void load()}><RefreshCw size={16} /></button></section>
    <ErrorNotice message={error} />
    <div className="settings-layout">
      <nav className="settings-tabs" aria-label="设置分组">{tabs.filter(([key]) => admin || key === 'data').map(([key, label, Icon]) => <button key={key} className={tab === key ? 'active' : ''} onClick={() => setTab(key)}><Icon size={16} />{label}</button>)}</nav>
      {loading ? <Loading label="加载设置" /> : <motion.div key={tab} initial={reduceMotion ? false : { opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} className="settings-content">
        {tab === 'system' && system && <Panel title="单一 job-worker" eyebrow="RUNTIME"><form className="settings-form" onSubmit={saveSystem}><div className="setting-state"><span><strong>执行拓扑</strong><small>研究、回测、交易方案、个人档案、退出诊断和 Jev 训练共用一个隔离队列 Worker。</small></span><StatusPill status={system.actual_loaded_mode || 'UNKNOWN'} /></div><label>LLM Agent 并发<input value={String(system.values.llm_agent_max_concurrency ?? 4)} readOnly /></label><label>低内存模式<input value={String(system.values.api_runtime_mode ?? 'LIGHTWEIGHT')} readOnly /></label><label>Jev 后端<select value={String(system.values.jev_backend ?? 'local')} onChange={(event) => setSystem({ ...system, values: { ...system.values, jev_backend: event.target.value } })}><option value="local">本地部署</option><option value="live">Jev Live 云端 API</option></select></label><label>Jev Live Base URL<input type="url" value={String(system.values.jev_live_base_url ?? '')} placeholder="https://live.example.com" onChange={(event) => setSystem({ ...system, values: { ...system.values, jev_live_base_url: event.target.value } })} /></label><div className="form-grid"><label>Jev Live 模型<input value={String(system.values.jev_live_model ?? 'jev-live')} onChange={(event) => setSystem({ ...system, values: { ...system.values, jev_live_model: event.target.value } })} /></label><label>API 路径<input value={String(system.values.jev_live_endpoint ?? '/v1/decisions')} onChange={(event) => setSystem({ ...system, values: { ...system.values, jev_live_endpoint: event.target.value } })} /></label></div><div className="form-grid"><label>Live API Key<input type="password" value={jevApiKey} placeholder="已配置则留空保持不变" onChange={(event) => setJevApiKey(event.target.value)} autoComplete="new-password" /></label><label>Live 超时<input type="number" min="1" max="120" value={Number(system.values.jev_live_timeout_seconds ?? 30)} onChange={(event) => setSystem({ ...system, values: { ...system.values, jev_live_timeout_seconds: Number(event.target.value) } })} /></label></div><label>Jev 置信度阈值<input type="number" min="0" max="1" step="0.01" value={Number(system.values.jev_confidence_threshold ?? 0.6)} onChange={(event) => setSystem({ ...system, values: { ...system.values, jev_confidence_threshold: Number(event.target.value) } })} /></label><label className="checkbox-row"><input type="checkbox" checked={Boolean(system.values.decision_system2_enabled ?? true)} onChange={(event) => setSystem({ ...system, values: { ...system.values, decision_system2_enabled: event.target.checked } })} />启用低置信度 System-2 诊断</label><label>管理员密码<input type="password" value={unlockPassword} onChange={(event) => setUnlockPassword(event.target.value)} autoComplete="current-password" /></label><button className="primary" disabled={busy}>保存运行配置</button></form></Panel>}
        {tab === 'model' && <Panel title="研究模型" eyebrow="ENCRYPTED PROVIDER"><form className="settings-form" onSubmit={saveModel}><label>Base URL<input type="url" value={draft.base_url} onChange={(event) => setDraft({ ...draft, base_url: event.target.value })} required /></label><label>API Key<input type="password" value={draft.api_key} placeholder={model?.api_key_configured ? '已配置，留空保持不变' : '输入 API Key'} onChange={(event) => setDraft({ ...draft, api_key: event.target.value })} /></label><div className="form-grid"><label>模型<input value={draft.research_model} onChange={(event) => setDraft({ ...draft, research_model: event.target.value })} required /></label><label>推理强度<select value={draft.research_reasoning_effort} onChange={(event) => setDraft({ ...draft, research_reasoning_effort: event.target.value })}><option value="low">low</option><option value="medium">medium</option><option value="high">high</option><option value="xhigh">xhigh</option></select></label></div><label>超时秒数<input type="number" min="1" max="600" value={draft.timeout_seconds} onChange={(event) => setDraft({ ...draft, timeout_seconds: Number(event.target.value) })} /></label><button className="primary" disabled={busy}>保存并探测</button></form><div className="model-status"><StatusPill status={model?.reachable ? 'ACTIVE' : model?.configured ? 'WARNING' : 'PENDING'} /><span>{model?.status_message || '尚未配置'}</span></div></Panel>}
        {tab === 'jev' && <><Panel title="Jev 运行状态" eyebrow="SYSTEM-1" action={<button className="primary icon-text" disabled={busy || ['queued', 'running'].includes(training?.status || '')} onClick={() => void triggerTraining()}><Play size={15} />触发训练</button>}><div className="jev-status"><div><strong>模型版本</strong><span>{system?.values.jev_model_version || 'jev-baseline-v1'}</span></div><div><strong>置信度阈值</strong><span>{String(system?.values.jev_confidence_threshold ?? '0.60')}</span></div><div><strong>System-2</strong><StatusPill status={system?.values.decision_system2_enabled === false ? 'DISABLED' : 'ACTIVE'} /></div><div><strong>训练任务</strong><StatusPill status={training?.status || 'IDLE'} /></div></div>{training && training.training_id !== 'none' && <div className="training-progress"><span>{training.training_id}</span><progress max={1} value={training.progress} /><small>{Math.round(training.progress * 100)}% · {training.error || training.status}</small></div>}</Panel><Panel title="可用模型" eyebrow="MODEL REGISTRY">{jevModels.length ? <div className="user-table">{jevModels.map((item) => <div className="user-row" key={item.version}><span><strong>{item.version}</strong><small>{item.path}</small></span><StatusPill status={item.version === system?.values.jev_model_version ? 'ACTIVE' : 'PENDING'} /></div>)}</div> : <Empty title="暂无 Jev 模型" />}</Panel><Panel title="训练历史" eyebrow="TRAINING HISTORY">{trainingHistory?.trainings.length ? <div className="run-list">{trainingHistory.trainings.map((item) => <div className="run-row" key={item.training_id}><div><strong>{item.training_id}</strong><small>{item.completed_at ? formatTime(item.completed_at) : formatTime(item.started_at || undefined)}</small></div><StatusPill status={item.status} /></div>)}</div> : <Empty title="暂无训练记录" />}</Panel></>}
        {tab === 'account' && <div className="settings-account"><Panel title="创建内部账户" eyebrow="NEW USER"><form className="settings-form" onSubmit={createUser}><label>用户名<input minLength={3} value={newUser.username} onChange={(event) => setNewUser({ ...newUser, username: event.target.value })} required /></label><label>初始密码<input type="password" minLength={10} value={newUser.password} onChange={(event) => setNewUser({ ...newUser, password: event.target.value })} required /></label><label>角色<select value={newUser.role} onChange={(event) => setNewUser({ ...newUser, role: event.target.value })}><option value="USER">研究员</option><option value="ADMIN">管理员</option></select></label><button className="primary icon-text" disabled={busy}><UserPlus size={15} />创建账户</button></form></Panel><Panel title="账户与权限" eyebrow="ACCESS CONTROL" action={<span className="count-badge">{users.length}</span>}>{users.length ? <div className="user-table">{users.map((account) => { const disabled = Boolean(account.disabled || account.enabled === false || account.is_active === false); const protectedAccount = Boolean(account.is_admin_account); const self = account.username === user?.username; return <div className="user-row" key={account.id || account.user_id || account.username}><span><strong>{account.username}</strong><small>{account.role} · {formatTime(account.created_at)}</small></span><span className="row-actions"><StatusPill status={disabled ? 'DISABLED' : 'ACTIVE'} /><button className="icon-button" title={disabled ? '启用用户' : '禁用用户'} disabled={protectedAccount || self} onClick={() => void changeUser(account, 'toggle')}>{disabled ? <CheckCircle2 size={15} /> : <Ban size={15} />}</button><button className="icon-button" title="重置密码" onClick={() => void changeUser(account, 'reset')}><RotateCcw size={15} /></button><button className="icon-button danger" title="删除用户" disabled={protectedAccount || self} onClick={() => void changeUser(account, 'delete')}><Trash2 size={15} /></button></span></div> })}</div> : <Empty title="暂无用户" />}</Panel></div>}
        {tab === 'data' && <><div className="split-grid"><Panel title="导出个人档案" eyebrow="ENCRYPTED EXPORT"><p className="form-hint">导出包包含持仓、自选、研究和文字记录，不含凭据、缓存或图片。</p><label>一次性口令<input type="password" minLength={8} value={archivePassphrase} onChange={(event) => setArchivePassphrase(event.target.value)} autoComplete="new-password" /></label><button className="primary icon-text" disabled={busy || archivePassphrase.length < 8} onClick={() => void startExport()}><Download size={15} />生成加密档案</button>{exportJob && <div className="archive-job"><strong>{exportJob.status === 'SUCCEEDED' ? '档案已就绪' : `正在处理：${exportJob.phase}`}</strong><progress max={100} value={exportJob.progress} /><div className="row-actions">{exportJob.status === 'SUCCEEDED' && <button className="secondary icon-text" onClick={() => void downloadExport()}><Download size={15} />下载</button>}<button className="danger-button" onClick={() => void api.deletePersonalExport(exportJob.archive_id).then(() => setExportJob(null))}><Trash2 size={14} />删除</button></div></div>}</Panel><Panel title="导入并预览" eyebrow="VALIDATE BEFORE MERGE"><label>档案文件<input type="file" accept=".ashare,application/vnd.ashare.personal-profile" onChange={(event) => setImportFile(event.target.files?.[0] || null)} /></label><label>一次性口令<input type="password" value={importPassphrase} onChange={(event) => setImportPassphrase(event.target.value)} autoComplete="current-password" /></label><button className="primary icon-text" disabled={busy || !importFile || importPassphrase.length < 8} onClick={() => void startImport()}><Upload size={15} />生成预览</button>{importJob && <div className="archive-job"><strong>{importJob.status === 'SUCCEEDED' ? '预览已生成' : `正在处理：${importJob.phase}`}</strong><progress max={100} value={importJob.progress} /></div>}</Panel></div>{importJob?.status === 'SUCCEEDED' && <Panel title="分类合并预览" eyebrow="CONFLICT REVIEW"><div className="archive-preview-grid"><section><strong>自选股并集</strong><p>新增 {(preview.watchlist?.new || []).length} 项，重复 {(preview.watchlist?.duplicate || []).length} 项。</p></section><section><strong>历史记录</strong><p>{Object.entries(preview.history || {}).map(([key, value]) => `${key} ${value}`).join(' · ') || '无历史记录'}</p></section><section><strong>总资产</strong><p>当前：{preview.total_assets?.current ?? '未设置'} · 导入：{preview.total_assets?.imported ?? '未设置'}</p><select value={String(mergeOptions.total_assets || 'CURRENT')} onChange={(event) => setMergeOptions({ ...mergeOptions, total_assets: event.target.value })}><option value="CURRENT">保留当前值</option><option value="IMPORTED">使用导入值</option></select></section></div>{conflicts.map((conflict) => <div className="archive-conflict" key={conflict.symbol}><strong>{conflict.symbol} 持仓冲突</strong><pre>当前：{JSON.stringify(conflict.current)}{`\n`}导入：{JSON.stringify(conflict.imported)}</pre><select value={String((mergeOptions.positions as Record<string, string> | undefined)?.[conflict.symbol] || 'CURRENT')} onChange={(event) => setMergeOptions({ ...mergeOptions, positions: { ...((mergeOptions.positions as Record<string, string> | undefined) || {}), [conflict.symbol]: event.target.value } })}><option value="CURRENT">保留当前持仓</option><option value="IMPORTED">使用导入持仓</option></select></div>)}<button className="primary" disabled={busy || activeJob(applyJob)} onClick={() => void applyImport()}>确认并异步应用</button>{applyJob && <div className="archive-job"><strong>{applyJob.status === 'SUCCEEDED' ? '合并完成' : `合并状态：${applyJob.phase}`}</strong><progress max={100} value={applyJob.progress} /></div>}</Panel>}</>}
        {message && <div className="snapshot-isolation">{message}</div>}
      </motion.div>}
    </div>
  </div>
}
