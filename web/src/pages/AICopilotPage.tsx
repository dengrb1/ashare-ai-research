import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import { BrainCircuit, ChevronDown, Send, Sparkles } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { api, streamAIChat } from '../api'
import type { AIChatMessage, AIChatThread } from '../types'
import { Empty, Loading, Panel, StatusPill, formatTime } from '../components/Ui'

export function AICopilotPage() {
  const reduceMotion = useReducedMotion()
  const [threads, setThreads] = useState<AIChatThread[]>([])
  const [active, setActive] = useState<AIChatThread | null>(null)
  const [messages, setMessages] = useState<AIChatMessage[]>([])
  const [models, setModels] = useState<string[]>([])
  const [model, setModel] = useState('gpt-5.6-sol')
  const [webSearch, setWebSearch] = useState(false)
  const [effort, setEffort] = useState('medium')
  const [content, setContent] = useState('')
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState('READY')
  const [error, setError] = useState<string | null>(null)
  const sortedThreads = useMemo(() => [...threads].sort((a, b) => b.updated_at.localeCompare(a.updated_at)), [threads])

  useEffect(() => { void Promise.all([api.aiChatThreads(), api.aiModels()]).then(([loadedThreads, options]) => { setThreads(loadedThreads); setModels(options.models); setModel(options.models[0] || model); if (loadedThreads[0]) setActive(loadedThreads[0]) }).catch((reason) => setError(reason instanceof Error ? reason.message : 'AI 助手加载失败')) }, [])
  useEffect(() => { if (!active) return; setMessages([]); void api.aiChatMessages(active.thread_id).then(setMessages).catch(() => undefined) }, [active])

  async function createThread() {
    try { const thread = await api.createAIChatThread(); setThreads((items) => [thread, ...items]); setActive(thread); setMessages([]) } catch (reason) { setError(reason instanceof Error ? reason.message : '无法创建对话') }
  }
  async function send(event: React.FormEvent) {
    event.preventDefault()
    if (!content.trim() || busy) return
    let thread = active
    if (!thread) { thread = await api.createAIChatThread(); setThreads((items) => [thread!, ...items]); setActive(thread) }
    const text = content.trim(); setContent(''); setBusy(true); setStatus('RUNNING'); setError(null)
    const optimistic: AIChatMessage = { message_id: `local-${Date.now()}`, thread_id: thread.thread_id, role: 'user', content: text, status: 'COMPLETED', mentioned_symbols: [], sources: [], cache_hit: false, input_tokens: 0, output_tokens: 0, created_at: new Date().toISOString() }
    setMessages((items) => [...items, optimistic])
    try {
      await streamAIChat(thread.thread_id, { content: text, model, reasoning_effort: effort, web_search: webSearch }, (event) => {
        if (event.type === 'delta') setMessages((items) => { const last = items.at(-1); if (last?.role === 'assistant' && last.message_id === 'streaming') return [...items.slice(0, -1), { ...last, content: `${last.content}${String(event.content || event.delta || '')}` }]; return [...items, { message_id: 'streaming', thread_id: thread!.thread_id, role: 'assistant', content: String(event.content || event.delta || ''), status: 'STREAMING', mentioned_symbols: [], sources: [], cache_hit: false, input_tokens: 0, output_tokens: 0, created_at: new Date().toISOString() }] })
        if (event.type === 'done') setStatus('READY')
      })
    } catch (reason) { setStatus('DEGRADED'); setError(reason instanceof Error ? reason.message : 'AI 诊断暂不可用') } finally { setBusy(false) }
  }

  return <div className="page-stack copilot-page"><section className="copilot-head"><div><div className="eyebrow">AI COPILOT</div><h2>研究副驾驶</h2><p>Jev System-1 默认裁决，低置信度时异步交给 System-2 做主动诊断。</p></div><div className="copilot-status"><span><i className="status-dot green" />Jev System-1</span><StatusPill status={status} /><span className="fallback-chip"><Sparkles size={14} />System-2 兜底待命</span></div></section><div className="copilot-layout"><Panel title="对话" eyebrow="DIAGNOSTIC CHAT" className="chat-panel"><div className="chat-toolbar"><button className="secondary" onClick={() => void createThread()}>新对话</button><label>模型<select value={model} onChange={(event) => setModel(event.target.value)}>{(models.length ? models : [model]).map((item) => <option value={item} key={item}>{item}</option>)}</select></label><label>推理<select value={effort} onChange={(event) => setEffort(event.target.value)}><option value="low">low</option><option value="medium">medium</option><option value="high">high</option><option value="xhigh">xhigh</option></select></label></div><div className="chat-messages"><AnimatePresence initial={false}>{messages.map((message) => <motion.article layout={!reduceMotion} initial={reduceMotion ? false : { opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} className={`chat-message ${message.role}`} key={message.message_id}><div className="message-avatar">{message.role === 'assistant' ? <BrainCircuit size={15} /> : '你'}</div><div><p>{message.content || (message.status === 'STREAMING' ? '…' : '')}</p><small>{formatTime(message.created_at)} {message.cache_hit ? '· 缓存' : ''}</small></div></motion.article>)}</AnimatePresence>{!messages.length && <Empty title="开始一次诊断" description="询问标的、研究结果或风险证据" />}</div><form className="chat-composer" onSubmit={send}><textarea value={content} onChange={(event) => setContent(event.target.value)} placeholder="描述你要复核的标的或研究结果" rows={3} /><button className="primary icon-button" aria-label="发送" disabled={busy || !content.trim()}>{busy ? <Loading label="" /> : <Send size={17} />}</button></form>{error && <div className="inline-error">{error}</div>}</Panel><Panel title="运行状态" eyebrow="SYSTEM ROUTING"><div className="copilot-state"><div><span className="state-icon"><BrainCircuit size={18} /></span><div><strong>System-1 · Jev</strong><small>确定性裁决器，输出结构化置信度</small></div><StatusPill status="ACTIVE" /></div><div><span className="state-icon coral"><Sparkles size={18} /></span><div><strong>System-2 · LLM</strong><small>仅在低置信度或 Jev 不可用时触发</small></div><StatusPill status={status === 'DEGRADED' ? 'WARNING' : 'PENDING'} /></div></div><div className="confidence-meter"><span>当前对话状态</span><strong>{status === 'RUNNING' ? '诊断中' : status === 'DEGRADED' ? '已降级' : '就绪'}</strong><div><i style={{ width: status === 'DEGRADED' ? '45%' : status === 'RUNNING' ? '72%' : '100%' }} /></div></div></Panel><Panel title="历史对话" eyebrow="THREADS"><div className="thread-list">{sortedThreads.length ? sortedThreads.map((thread) => <button className={active?.thread_id === thread.thread_id ? 'active' : ''} key={thread.thread_id} onClick={() => setActive(thread)}><span>{thread.title}</span><ChevronDown size={14} /></button>) : <Empty title="暂无历史对话" />}</div></Panel></div></div>
}
