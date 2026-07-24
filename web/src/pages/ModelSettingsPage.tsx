import { useEffect, useState, type FormEvent } from 'react'
import { Navigate } from 'react-router-dom'
import { api } from '../api'
import { ErrorNotice, formatTime, Panel, StatusPill } from '../components/Ui'
import { useAuth } from '../context/AuthContext'
import type { ModelProfile, ModelSettings, ModelSettingsDraft } from '../types'

function defaultProfile(model: string): ModelProfile {
  return {
    model,
    cache_policy: 'COMPATIBLE',
    context_window_tokens: 128000,
    output_token_reserve: 8192,
    reasoning_token_reserve: 0,
    input_price_per_million: 0,
    cached_input_price_per_million: 0,
    cache_write_price_per_million: 0,
    output_price_per_million: 0,
  }
}

const DEFAULTS: ModelSettingsDraft = {
  base_url: '',
  api_key: '',
  search_model: 'gpt-5.6-luna',
  search_reasoning_effort: 'low',
  research_model: 'gpt-5.6-sol',
  research_reasoning_effort: 'high',
  model_profiles: [defaultProfile('gpt-5.6-luna'), defaultProfile('gpt-5.6-sol')],
  timeout_seconds: 90,
  enabled: true,
}

export function ModelSettingsPage() {
  const { user } = useAuth()
  const isAdmin = user?.role?.toLowerCase() === 'admin'
  const [current, setCurrent] = useState<ModelSettings | null>(null)
  const [form, setForm] = useState<ModelSettingsDraft>(DEFAULTS)
  const [models, setModels] = useState<string[]>([])
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState('')

  useEffect(() => {
    if (!isAdmin) return
    api.modelSettings().then((value) => {
      setCurrent(value)
      setForm({
        base_url: value.base_url,
        api_key: '',
        search_model: value.search_model,
        search_reasoning_effort: value.search_reasoning_effort,
        research_model: value.research_model,
        research_reasoning_effort: value.research_reasoning_effort,
        model_profiles: value.model_profiles.length ? value.model_profiles : [defaultProfile(value.search_model), defaultProfile(value.research_model)],
        timeout_seconds: value.timeout_seconds,
        enabled: value.enabled,
      })
    }).catch((reason) => setError(reason instanceof Error ? reason.message : '模型设置加载失败'))
  }, [isAdmin])

  if (!isAdmin) return <Navigate to="/" replace />

  function updateProfile(index: number, patch: Partial<ModelProfile>) {
    setForm((value) => ({ ...value, model_profiles: value.model_profiles.map((profile, current) => current === index ? { ...profile, ...patch } : profile) }))
  }

  async function run(action: 'test' | 'models' | 'save', event?: FormEvent) {
    event?.preventDefault()
    setBusy(action); setError(''); setMessage('')
    try {
      if (action === 'test') {
        const result = await api.testModelSettings(form)
        setMessage(`${result.model} 连通成功：${result.message}`)
      } else if (action === 'models') {
        const result = await api.listModels(form)
        setModels(result.models)
        setMessage(`已读取 ${result.models.length} 个可用模型`)
      } else {
        const result = await api.saveModelSettings(form)
        setCurrent(result)
        setForm({ ...form, api_key: '' })
        setMessage(`模型配置 v${result.version} 已启用`)
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '模型操作失败')
    } finally {
      setBusy('')
    }
  }

  return <div className="admin-layout">
    <Panel title="当前模型状态" eyebrow="MODEL CONTROL PLANE" action={<StatusPill status={current?.reachable ? 'ONLINE' : current?.configured ? 'DEGRADED' : 'UNCONFIGURED'} />}>
      <div className="search-result-meta">
        <div><span>配置版本</span><strong>{current?.configured ? `v${current.version}` : '未配置'}</strong></div>
        <div><span>连通性</span><strong>{current?.reachable ? '可达' : current?.configured ? '已配置 / 不可达' : '未配置'}</strong></div>
        <div><span>搜索模型</span><strong>{current?.search_model || 'gpt-5.6-luna'}</strong></div>
        <div><span>研究模型</span><strong>{current?.research_model || 'gpt-5.6-sol'}</strong></div>
        <div><span>最近检测</span><strong>{formatTime(current?.checked_at || undefined)}</strong></div>
      </div>
      <p className="form-hint">{current?.status_message || 'API Key 不会回传到浏览器；留空表示继续使用已加密保存的密钥。'}</p>
    </Panel>

    <Panel title="OpenAI-compatible API" eyebrow="ENCRYPTED VERSIONED SETTINGS">
      <form className="run-form" onSubmit={(event) => void run('save', event)}>
        <label>Base URL<input value={form.base_url} onChange={(event) => setForm({ ...form, base_url: event.target.value })} placeholder="https://gateway.example.com/v1" required /></label>
        <label>API Key<input type="password" autoComplete="new-password" value={form.api_key} onChange={(event) => setForm({ ...form, api_key: event.target.value })} placeholder={current?.api_key_configured ? '已加密保存；留空不修改' : '请输入 API Key'} /></label>
        <label>搜索模型<input list="available-models" value={form.search_model} onChange={(event) => setForm({ ...form, search_model: event.target.value })} required /></label>
        <label>搜索推理强度<select value={form.search_reasoning_effort} onChange={(event) => setForm({ ...form, search_reasoning_effort: event.target.value })}><option value="low">low</option><option value="medium">medium</option><option value="high">high</option><option value="xhigh">xhigh</option></select></label>
        <label>研究模型<input list="available-models" value={form.research_model} onChange={(event) => setForm({ ...form, research_model: event.target.value })} required /></label>
        <label>研究推理强度<select value={form.research_reasoning_effort} onChange={(event) => setForm({ ...form, research_reasoning_effort: event.target.value })}><option value="low">low</option><option value="medium">medium</option><option value="high">high</option><option value="xhigh">xhigh</option></select></label>
        <label>超时秒数<input type="number" min={1} max={600} value={form.timeout_seconds} onChange={(event) => setForm({ ...form, timeout_seconds: Number(event.target.value) })} /></label>
        <label><input type="checkbox" checked={form.enabled} onChange={(event) => setForm({ ...form, enabled: event.target.checked })} /> 启用此配置</label>
        <div className="model-profile-editor">
          {form.model_profiles.map((profile, index) => <fieldset key={`${profile.model}-${index}`}>
            <legend>{profile.model || `模型 ${index + 1}`}</legend>
            <label>模型<input list="available-models" value={profile.model} onChange={(event) => updateProfile(index, { model: event.target.value })} required /></label>
            <label>缓存档案<select value={profile.cache_policy} onChange={(event) => updateProfile(index, { cache_policy: event.target.value as ModelProfile['cache_policy'] })}><option value="GROK">GROK</option><option value="OPENAI">OPENAI</option><option value="COMPATIBLE">COMPATIBLE</option></select></label>
            <label>上下文窗口<input type="number" min={1024} max={4000000} value={profile.context_window_tokens} onChange={(event) => updateProfile(index, { context_window_tokens: Number(event.target.value) })} /></label>
            <label>输出预留<input type="number" min={0} value={profile.output_token_reserve} onChange={(event) => updateProfile(index, { output_token_reserve: Number(event.target.value) })} /></label>
            <label>推理预留<input type="number" min={0} value={profile.reasoning_token_reserve} onChange={(event) => updateProfile(index, { reasoning_token_reserve: Number(event.target.value) })} /></label>
            <label>输入 / 百万<input type="number" min={0} step="0.000001" value={profile.input_price_per_million} onChange={(event) => updateProfile(index, { input_price_per_million: event.target.value })} /></label>
            <label>缓存读取 / 百万<input type="number" min={0} step="0.000001" value={profile.cached_input_price_per_million} onChange={(event) => updateProfile(index, { cached_input_price_per_million: event.target.value })} /></label>
            <label>缓存写入 / 百万<input type="number" min={0} step="0.000001" value={profile.cache_write_price_per_million} onChange={(event) => updateProfile(index, { cache_write_price_per_million: event.target.value })} /></label>
            <label>输出 / 百万<input type="number" min={0} step="0.000001" value={profile.output_price_per_million} onChange={(event) => updateProfile(index, { output_price_per_million: event.target.value })} /></label>
          </fieldset>)}
        </div>
        <datalist id="available-models">{models.map((model) => <option key={model} value={model} />)}</datalist>
        <div className="row-actions"><button type="button" className="secondary" disabled={!!busy} onClick={() => void run('models')}>{busy === 'models' ? '读取中…' : '读取模型列表'}</button><button type="button" className="secondary" disabled={!!busy} onClick={() => void run('test')}>{busy === 'test' ? '测试中…' : '测试连接'}</button><button className="primary" disabled={!!busy}>{busy === 'save' ? '验证并保存…' : '验证并启用新版本'}</button></div>
        <ErrorNotice message={error} />
        {message && <div className="snapshot-isolation">{message}</div>}
        <p className="form-hint">启用配置时会先执行严格 JSON Schema 的 Responses API 探测；失败时旧版本继续生效。</p>
      </form>
    </Panel>
  </div>
}
