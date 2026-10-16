import { motion, useReducedMotion } from 'framer-motion'
import { ArrowDown, ArrowUp, Ban, CheckCircle2, Copy, Download, KeyRound, Plus, RefreshCw, RotateCcw, ShieldCheck, Trash2, Upload, UserCog, UserPlus, Wrench, SlidersHorizontal, X } from 'lucide-react'
import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { api, downloadPersonalArchive } from '../api'
import { useAuth } from '../context/AuthContext'
import type { FactorEvolutionSchedule, FactorGenomeVersion, ModelSettings, PersonalArchiveJob, SystemSettings, User } from '../types'
import { Empty, ErrorNotice, Loading, Panel, StatusPill, formatTime } from '../components/Ui'

type SettingsTab = 'system' | 'model' | 'evolution' | 'account' | 'data'

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
  const [probeLogs, setProbeLogs] = useState<import('../types').ModelProbeLog[]>([])
  const [users, setUsers] = useState<User[]>([])
  const [activeGenome, setActiveGenome] = useState<FactorGenomeVersion | null>(null)
  const [candidates, setCandidates] = useState<FactorGenomeVersion[]>([])
  const [schedule, setSchedule] = useState<FactorEvolutionSchedule | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [unlockPassword, setUnlockPassword] = useState('')
  const [unlockToken, setUnlockToken] = useState('')
  const [archivePassphrase, setArchivePassphrase] = useState('')
  const [importPassphrase, setImportPassphrase] = useState('')
  const [importFile, setImportFile] = useState<File | null>(null)
  const [exportJob, setExportJob] = useState<PersonalArchiveJob | null>(null)
  const [importJob, setImportJob] = useState<PersonalArchiveJob | null>(null)
  const [applyJob, setApplyJob] = useState<PersonalArchiveJob | null>(null)
  const [mergeOptions, setMergeOptions] = useState<Record<string, unknown>>({})
  const [newUser, setNewUser] = useState({ username: '', password: '', role: 'USER' })
  const [draft, setDraft] = useState({ research_model: 'gpt-5.6-sol', research_reasoning_effort: 'high', base_url: '', api_key: '', timeout_seconds: 90, enabled: true, model_profiles: [] as ModelSettings['model_profiles'], providers: [] as Array<Record<string, unknown>>, primary_provider_id: null as string | null, fallback_provider_ids: [] as string[], routing: {} as Record<string, string> })

  const updateProvider = (index: number, values: Record<string, unknown>) => setDraft((current) => ({ ...current, providers: current.providers.map((provider, itemIndex) => itemIndex === index ? { ...provider, ...values } : provider) }))
  const addProvider = () => setDraft((current) => {
    const provider = { provider_id: `provider-${current.providers.length + 1}`, name: '新供应商', base_url: current.base_url, model: current.research_model, enabled: true, priority: current.providers.length }
    return { ...current, providers: [...current.providers, provider], primary_provider_id: current.primary_provider_id || provider.provider_id }
  })
  const duplicateProvider = (index: number) => setDraft((current) => ({ ...current, providers: [...current.providers.slice(0, index + 1), { ...current.providers[index], provider_id: `${String(current.providers[index].provider_id || 'provider')}-copy` }, ...current.providers.slice(index + 1)] }))
  const removeProvider = (index: number) => setDraft((current) => {
    const removedId = String(current.providers[index]?.provider_id || '')
    const providers = current.providers.filter((_, itemIndex) => itemIndex !== index)
    const fallback = current.fallback_provider_ids.filter((id) => id !== removedId && providers.some((provider) => String(provider.provider_id || '') === id))
    const primary = current.primary_provider_id === removedId ? String(providers[0]?.provider_id || '') || null : current.primary_provider_id
    return { ...current, providers, primary_provider_id: primary, fallback_provider_ids: fallback }
  })
  const moveProvider = (index: number, direction: -1 | 1) => setDraft((current) => { const next = [...current.providers]; const target = index + direction; if (target < 0 || target >= next.length) return current; [next[index], next[target]] = [next[target], next[index]]; return { ...current, providers: next.map((provider, itemIndex) => ({ ...provider, priority: itemIndex })) } })

  async function probeProvider(index: number) {
    setBusy(true); setError(''); setMessage(null)
    try {
      const provider = draft.providers[index]
      await api.testModelSettings({ ...draft, providers: [provider] })
      setMessage(`${String(provider.name || provider.provider_id || '供应商')} 探测成功`)
    } catch (reason) { setError(reason instanceof Error ? reason.message : '供应商探测失败') } finally { setBusy(false) }
  }

  const loadAdmin = async () => {
    if (!admin) return
    const [systemSettings, modelSettings, accountUsers, active, versions, evolutionSchedule, logs] = await Promise.all([
      api.systemSettings(), api.modelSettings(), api.users(), api.factorEvolutionActive(), api.factorEvolutionCandidates(), api.factorEvolutionSchedule(), api.modelProbeLogs(20),
    ])
    setSystem(systemSettings); setModel(modelSettings); setUsers(accountUsers); setActiveGenome(active); setCandidates(versions.versions || []); setSchedule(evolutionSchedule)
    setProbeLogs(logs)
    setDraft((current) => ({ ...current, research_model: modelSettings.research_model, research_reasoning_effort: modelSettings.research_reasoning_effort, base_url: modelSettings.base_url, timeout_seconds: modelSettings.timeout_seconds, enabled: modelSettings.enabled, model_profiles: modelSettings.model_profiles, providers: modelSettings.providers || [], primary_provider_id: modelSettings.primary_provider_id || null, fallback_provider_ids: modelSettings.fallback_provider_ids || [], routing: modelSettings.routing || {} }))
  }

  const load = async () => {
    setLoading(true); setError('')
    try { await loadAdmin() } catch (reason) { setError(reason instanceof Error ? reason.message : '设置加载失败') } finally { setLoading(false) }
  }
  useEffect(() => { void load() }, [admin])

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
        factor_confidence_threshold: Number(system.values.factor_confidence_threshold ?? 0.6),
        decision_system2_enabled: Boolean(system.values.decision_system2_enabled ?? true),
        strategy_evolution_enabled: Boolean(system.values.strategy_evolution_enabled ?? true),
        strategy_evolution_weekday: Number(system.values.strategy_evolution_weekday ?? 5),
        strategy_evolution_hour: Number(system.values.strategy_evolution_hour ?? 20),
        strategy_evolution_min_samples: Number(system.values.strategy_evolution_min_samples ?? 60),
        strategy_evolution_max_drawdown: Number(system.values.strategy_evolution_max_drawdown ?? 0.2),
        strategy_evolution_max_turnover: Number(system.values.strategy_evolution_max_turnover ?? 20),
        strategy_evolution_min_improvement: Number(system.values.strategy_evolution_min_improvement ?? 0),
        strategy_evolution_max_candidates: Number(system.values.strategy_evolution_max_candidates ?? 8),
      }
      setSystem(await api.saveSystemSettings(payload, token))
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

  async function reviewCandidate(candidateId: string, action: 'approve' | 'reject') {
    setBusy(true); setError('')
    try {
      if (action === 'approve') await api.approveFactorCandidate(candidateId)
      else await api.rejectFactorCandidate(candidateId)
      const [active, versions] = await Promise.all([api.factorEvolutionActive(), api.factorEvolutionCandidates()])
      setActiveGenome(active); setCandidates(versions.versions || [])
      setMessage(action === 'approve' ? '候选因子版本已启用' : '候选因子版本已拒绝')
    } catch (reason) { setError(reason instanceof Error ? reason.message : '候选版本操作失败') } finally { setBusy(false) }
  }

  async function rollbackGenome() {
    setBusy(true); setError('')
    try {
      setActiveGenome(await api.rollbackFactorGenome())
      setCandidates((await api.factorEvolutionCandidates()).versions || [])
      setMessage('已回滚到上一个因子版本')
    } catch (reason) { setError(reason instanceof Error ? reason.message : '回滚失败') } finally { setBusy(false) }
  }

  async function toggleEvolution(enabled: boolean) {
    setBusy(true); setError('')
    try { setSchedule(await api.setFactorEvolutionEnabled(enabled)); setMessage(enabled ? '周度进化任务已启用' : '周度进化任务已停用') } catch (reason) { setError(reason instanceof Error ? reason.message : '进化任务开关失败') } finally { setBusy(false) }
  }

  const preview = useMemo(() => archivePreview(importJob), [importJob])
  const conflicts = preview.positions?.conflicts || []
  const tabs: Array<[SettingsTab, string, typeof Wrench]> = [['system', '系统与运行时', Wrench], ['model', '模型与密钥', KeyRound], ['evolution', '因子进化', SlidersHorizontal], ['account', '用户与权限', UserCog], ['data', '个人数据', ShieldCheck]]

  return <div className="page-stack settings-page">
    <section className="settings-head"><div><div className="eyebrow">CONTROL PLANE</div><h2>设置</h2><p>运行时、模型、因子进化和个人数据统一从这里管理。</p></div><button className="secondary icon-button" title="重新加载" onClick={() => void load()}><RefreshCw size={16} /></button></section>
    <ErrorNotice message={error} />
    <div className="settings-layout">
      <nav className="settings-tabs" aria-label="设置分组">{tabs.filter(([key]) => admin || key === 'data').map(([key, label, Icon]) => <button key={key} className={tab === key ? 'active' : ''} onClick={() => setTab(key)}><Icon size={16} />{label}</button>)}</nav>
      {loading ? <Loading label="加载设置" /> : <motion.div key={tab} initial={reduceMotion ? false : { opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} className="settings-content">
        {tab === 'system' && system && <Panel title="单一 job-worker" eyebrow="RUNTIME"><form className="settings-form" onSubmit={saveSystem}><div className="setting-state"><span><strong>执行拓扑</strong><small>研究、回测、交易方案、个人档案、退出诊断共用一个隔离队列 Worker。</small></span><StatusPill status={system.actual_loaded_mode || 'UNKNOWN'} /></div><label>LLM Agent 并发<input value={String(system.values.llm_agent_max_concurrency ?? 4)} readOnly /></label><label>低内存模式<input value={String(system.values.api_runtime_mode ?? 'LIGHTWEIGHT')} readOnly /></label><label>因子进化周度任务<input type="checkbox" checked={Boolean(system.values.strategy_evolution_enabled ?? true)} onChange={(e) => toggleEvolution(e.target.checked)} /></label><label>进化周度<input value={String(system.values.strategy_evolution_weekday ?? 5)} readOnly /></label><label>进化小时<input value={String(system.values.strategy_evolution_hour ?? 20)} readOnly /></label><label>进化最小样本<input value={String(system.values.strategy_evolution_min_samples ?? 60)} readOnly /></label><label>进化最大回撤<input value={String(system.values.strategy_evolution_max_drawdown ?? 0.2)} readOnly /></label><label>进化最大换手<input value={String(system.values.strategy_evolution_max_turnover ?? 20)} readOnly /></label><label>进化最小改善<input value={String(system.values.strategy_evolution_min_improvement ?? 0)} readOnly /></label><label>进化候选数量<input value={String(system.values.strategy_evolution_max_candidates ?? 8)} readOnly /></label><label>因子置信度阈值<input type="number" min="0" max="1" step="0.01" value={Number(system.values.factor_confidence_threshold ?? 0.6)} onChange={(event) => setSystem({ ...system, values: { ...system.values, factor_confidence_threshold: Number(event.target.value) } })} /></label><label className="checkbox-row"><input type="checkbox" checked={Boolean(system.values.decision_system2_enabled ?? true)} onChange={(event) => setSystem({ ...system, values: { ...system.values, decision_system2_enabled: event.target.checked } })} />启用低置信度 System-2 诊断</label><label>管理员密码<input type="password" value={unlockPassword} onChange={(event) => setUnlockPassword(event.target.value)} autoComplete="current-password" /></label><button className="primary" disabled={busy}>保存运行配置</button></form></Panel>}
        {tab === 'model' && <Panel title="AI 供应商" eyebrow="ENCRYPTED PROVIDERS"><form className="settings-form" onSubmit={saveModel}><label>兼容默认 Base URL<input type="url" value={draft.base_url} onChange={(event) => setDraft({ ...draft, base_url: event.target.value })} required /></label><label>默认 API Key<input type="password" value={draft.api_key} placeholder={model?.api_key_configured ? '已配置，留空保持不变' : '输入 API Key'} onChange={(event) => setDraft({ ...draft, api_key: event.target.value })} /></label><div className="form-grid"><label>默认模型<input value={draft.research_model} onChange={(event) => setDraft({ ...draft, research_model: event.target.value })} required /></label><label>推理强度<select value={draft.research_reasoning_effort} onChange={(event) => setDraft({ ...draft, research_reasoning_effort: event.target.value })}><option value="low">low</option><option value="medium">medium</option><option value="high">high</option><option value="xhigh">xhigh</option></select></label></div><label>超时秒数<input type="number" min="1" max="600" value={draft.timeout_seconds} onChange={(event) => setDraft({ ...draft, timeout_seconds: Number(event.target.value) })} /></label><div className="provider-editor"><div className="provider-editor-head"><strong>供应商与路由</strong><button type="button" className="secondary icon-text" onClick={addProvider}><Plus size={15} />新增</button></div>{draft.providers.length === 0 && <p className="form-hint">未添加供应商时使用兼容默认端点。</p>}{draft.providers.map((provider, index) => <div className="provider-row" key={`${String(provider.provider_id || 'provider')}-${index}`}><div className="form-grid"><label>ID<input value={String(provider.provider_id || '')} onChange={(event) => updateProvider(index, { provider_id: event.target.value })} /></label><label>名称<input value={String(provider.name || '')} onChange={(event) => updateProvider(index, { name: event.target.value })} /></label><label>Base URL<input type="url" value={String(provider.base_url || '')} onChange={(event) => updateProvider(index, { base_url: event.target.value })} required /></label><label>模型<input value={String(provider.model || '')} onChange={(event) => updateProvider(index, { model: event.target.value })} required /></label><label>API Key<input type="password" placeholder={provider.api_key_configured ? '已配置，留空保持不变' : '输入 API Key'} onChange={(event) => updateProvider(index, { api_key: event.target.value })} /></label><label>优先级<input type="number" min="0" value={Number(provider.priority || 0)} onChange={(event) => updateProvider(index, { priority: Number(event.target.value) })} /></label></div><div className="row-actions"><label className="checkbox-row"><input type="checkbox" checked={provider.enabled !== false} onChange={(event) => updateProvider(index, { enabled: event.target.checked })} />启用</label><button type="button" className="icon-button" title="上移" onClick={() => moveProvider(index, -1)}><ArrowUp size={15} /></button><button type="button" className="icon-button" title="下移" onClick={() => moveProvider(index, 1)}><ArrowDown size={15} /></button><button type="button" className="icon-button" title="复制" onClick={() => duplicateProvider(index)}><Copy size={15} /></button><button type="button" className="icon-button" title="测试此供应商" disabled={busy} onClick={() => void probeProvider(index)}><RefreshCw size={15} /></button><button type="button" className="icon-button danger" title="删除" onClick={() => removeProvider(index)}><Trash2 size={15} /></button></div></div>)}</div><div className="form-grid provider-routing"><label>主供应商<select value={draft.primary_provider_id || ''} onChange={(event) => setDraft({ ...draft, primary_provider_id: event.target.value || null })}><option value="">按优先级自动选择</option>{draft.providers.map((provider) => <option key={String(provider.provider_id)} value={String(provider.provider_id)}>{String(provider.name || provider.provider_id)}</option>)}</select></label><label>备用供应商（可多选）<select multiple value={draft.fallback_provider_ids} onChange={(event) => setDraft({ ...draft, fallback_provider_ids: Array.from(event.target.selectedOptions, (option) => option.value) })}>{draft.providers.map((provider) => <option key={String(provider.provider_id)} value={String(provider.provider_id)}>{String(provider.name || provider.provider_id)}</option>)}</select></label></div><div className="form-grid provider-routing">{['research', 'form_assist', 'chat', 'strategy', 'monitoring'].map((purpose) => <label key={purpose}>{purpose} 模型<input value={draft.routing[purpose] || ''} placeholder="沿用默认模型" onChange={(event) => setDraft({ ...draft, routing: { ...draft.routing, [purpose]: event.target.value } })} /></label>)}</div><button className="primary" disabled={busy}>保存并探测</button></form><div className="model-status"><StatusPill status={model?.reachable ? 'ACTIVE' : model?.configured ? 'WARNING' : 'PENDING'} /><span>{model?.status_message || '尚未配置'}</span><small>{(model?.providers || []).length ? `已配置 ${(model?.providers || []).length} 个供应商` : '兼容默认供应商'}</small></div>{probeLogs.length > 0 && <div className="probe-log-list"><strong>最近探测</strong>{probeLogs.slice(0, 8).map((log) => <div className="probe-log-row" key={log.log_id}><span>{log.purpose}</span><StatusPill status={log.outcome === 'SUCCEEDED' ? 'ACTIVE' : 'WARNING'} /><small>{log.model} · {log.message} · {formatTime(log.created_at)}</small></div>)}</div>}</Panel>}
        {tab === 'evolution' && activeGenome && <><Panel title="当前因子版本" eyebrow="ACTIVE"><div className="factor-status"><div><strong>版本</strong><span>{activeGenome.formula_version}</span></div><div><strong>参数哈希</strong><span>{activeGenome.parameter_sha256}</span></div><div><strong>状态</strong><StatusPill status={activeGenome.status} /></div><div><strong>批准时间</strong><span>{activeGenome.reviewed_at || '—'}</span></div></div><button className="primary icon-text" onClick={() => rollbackGenome()}><RotateCcw size={15} />回滚到上一个版本</button></Panel><Panel title="候选版本" eyebrow="CANDIDATES"><div className="run-list">{candidates.map((item) => <div key={item.candidate_id} className="run-row"><div><strong>{item.candidate_id}</strong><small>{item.formula_version}</small></div><StatusPill status={item.status} /><button className="secondary icon-text" onClick={() => reviewCandidate(item.candidate_id, 'approve')}><CheckCircle2 size={15} /></button><button className="secondary icon-text" onClick={() => reviewCandidate(item.candidate_id, 'reject')}><X size={15} /></button></div>)}</div></Panel><Panel title="进化日历" eyebrow="SCHEDULE"><label>启用周度进化任务<input type="checkbox" checked={Boolean(schedule?.enabled)} onChange={(e) => toggleEvolution(e.target.checked)} /></label><label>周几<input value={String(schedule?.weekday ?? 5)} readOnly /></label><label>小时<input value={String(schedule?.hour ?? 20)} readOnly /></label><label>最小样本<input value={String(schedule?.minimum_samples ?? 60)} readOnly /></label><label>最大回撤<input value={String(schedule?.maximum_drawdown ?? 0.2)} readOnly /></label><label>最大换手<input value={String(schedule?.maximum_turnover ?? 20)} readOnly /></label><label>最小改善<input value={String(schedule?.minimum_improvement ?? 0)} readOnly /></label><label>最大候选数量<input value={String(schedule?.maximum_candidates ?? 8)} readOnly /></label></Panel></>}
        {tab === 'account' && <div className="settings-account"><Panel title="创建内部账户" eyebrow="NEW USER"><form className="settings-form" onSubmit={createUser}><label>用户名<input minLength={3} value={newUser.username} onChange={(event) => setNewUser({ ...newUser, username: event.target.value })} required /></label><label>初始密码<input type="password" minLength={10} value={newUser.password} onChange={(event) => setNewUser({ ...newUser, password: event.target.value })} required /></label><label>角色<select value={newUser.role} onChange={(event) => setNewUser({ ...newUser, role: event.target.value })}><option value="USER">研究员</option><option value="ADMIN">管理员</option></select></label><button className="primary icon-text" disabled={busy}><UserPlus size={15} />创建账户</button></form></Panel><Panel title="账户与权限" eyebrow="ACCESS CONTROL" action={<span className="count-badge">{users.length}</span>}>{users.length ? <div className="user-table">{users.map((account) => { const disabled = Boolean(account.disabled || account.enabled === false || account.is_active === false); const protectedAccount = Boolean(account.is_admin_account); const self = account.username === user?.username; return <div className="user-row" key={account.id || account.user_id || account.username}><span><strong>{account.username}</strong><small>{account.role} · {formatTime(account.created_at)}</small></span><span className="row-actions"><StatusPill status={disabled ? 'DISABLED' : 'ACTIVE'} /><button className="icon-button" title={disabled ? '启用用户' : '禁用用户'} disabled={protectedAccount || self} onClick={() => void changeUser(account, 'toggle')}>{disabled ? <CheckCircle2 size={15} /> : <Ban size={15} />}</button><button className="icon-button" title="重置密码" onClick={() => void changeUser(account, 'reset')}><RotateCcw size={15} /></button><button className="icon-button danger" title="删除用户" disabled={protectedAccount || self} onClick={() => void changeUser(account, 'delete')}><Trash2 size={15} /></button></span></div> })}</div> : <Empty title="暂无用户" />}</Panel></div>}
        {tab === 'data' && <><div className="split-grid"><Panel title="导出个人档案" eyebrow="ENCRYPTED EXPORT"><p className="form-hint">导出包包含持仓、自选、研究和文字记录，不含凭据、缓存或图片。</p><label>一次性口令<input type="password" minLength={8} value={archivePassphrase} onChange={(event) => setArchivePassphrase(event.target.value)} autoComplete="new-password" /></label><button className="primary icon-text" disabled={busy || archivePassphrase.length < 8} onClick={() => void startExport()}><Download size={15} />生成加密档案</button>{exportJob && <div className="archive-job"><strong>{exportJob.status === 'SUCCEEDED' ? '档案已就绪' : `正在处理：${exportJob.phase}`}</strong><progress max={100} value={exportJob.progress} /><div className="row-actions">{exportJob.status === 'SUCCEEDED' && <button className="secondary icon-text" onClick={() => void downloadExport()}><Download size={15} />下载</button>}<button className="danger-button" onClick={() => void api.deletePersonalExport(exportJob.archive_id).then(() => setExportJob(null))}><Trash2 size={14} />删除</button></div></div>}</Panel><Panel title="导入并预览" eyebrow="VALIDATE BEFORE MERGE"><label>档案文件<input type="file" accept=".ashare,application/vnd.ashare.personal-profile" onChange={(event) => setImportFile(event.target.files?.[0] || null)} /></label><label>一次性口令<input type="password" value={importPassphrase} onChange={(event) => setImportPassphrase(event.target.value)} autoComplete="current-password" /></label><button className="primary icon-text" disabled={busy || !importFile || importPassphrase.length < 8} onClick={() => void startImport()}><Upload size={15} />生成预览</button>{importJob && <div className="archive-job"><strong>{importJob.status === 'SUCCEEDED' ? '预览已生成' : `正在处理：${importJob.phase}`}</strong><progress max={100} value={importJob.progress} /></div>}</Panel></div>{importJob?.status === 'SUCCEEDED' && <Panel title="分类合并预览" eyebrow="CONFLICT REVIEW"><div className="archive-preview-grid"><section><strong>自选股并集</strong><p>新增 {(preview.watchlist?.new || []).length} 项，重复 {(preview.watchlist?.duplicate || []).length} 项。</p></section><section><strong>历史记录</strong><p>{Object.entries(preview.history || {}).map(([key, value]) => `${key} ${value}`).join(' · ') || '无历史记录'}</p></section><section><strong>总资产</strong><p>当前：{preview.total_assets?.current ?? '未设置'} · 导入：{preview.total_assets?.imported ?? '未设置'}</p><select value={String(mergeOptions.total_assets || 'CURRENT')} onChange={(event) => setMergeOptions({ ...mergeOptions, total_assets: event.target.value })}><option value="CURRENT">保留当前值</option><option value="IMPORTED">使用导入值</option></select></section></div>{conflicts.map((conflict) => <div className="archive-conflict" key={conflict.symbol}><strong>{conflict.symbol} 持仓冲突</strong><pre>当前：{JSON.stringify(conflict.current)}{`\n`}导入：{JSON.stringify(conflict.imported)}</pre><select value={String((mergeOptions.positions as Record<string, string> | undefined)?.[conflict.symbol] || 'CURRENT')} onChange={(event) => setMergeOptions({ ...mergeOptions, positions: { ...((mergeOptions.positions as Record<string, string> | undefined) || {}), [conflict.symbol]: event.target.value } })}><option value="CURRENT">保留当前持仓</option><option value="IMPORTED">使用导入持仓</option></select></div>)}<button className="primary" disabled={busy || activeJob(applyJob)} onClick={() => void applyImport()}>确认并异步应用</button>{applyJob && <div className="archive-job"><strong>{applyJob.status === 'SUCCEEDED' ? '合并完成' : `合并状态：${applyJob.phase}`}</strong><progress max={100} value={applyJob.progress} /></div>}</Panel>}</>}
        {message && <div className="snapshot-isolation">{message}</div>}
      </motion.div>}
    </div>
  </div>
}
