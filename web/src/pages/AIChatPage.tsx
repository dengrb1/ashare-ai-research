import { useEffect, useRef, useState } from 'react'
import { api, streamAIChat } from '../api'
import { Empty, ErrorNotice, formatTime, Loading, Panel } from '../components/Ui'
import { useMarket } from '../context/MarketContext'
import type { AIChatMessage, AIChatThread } from '../types'

export function AIChatPage() {
  const { positions, watchlist } = useMarket()
  const [threads, setThreads] = useState<AIChatThread[]>([])
  const [thread, setThread] = useState<AIChatThread | null>(null)
  const [messages, setMessages] = useState<AIChatMessage[]>([])
  const [models, setModels] = useState<string[]>([])
  const [model, setModel] = useState('')
  const [effort, setEffort] = useState('medium')
  const [webSearch, setWebSearch] = useState(true)
  const [draft, setDraft] = useState('')
  const [streamed, setStreamed] = useState('')
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const abort = useRef<AbortController | null>(null)

  useEffect(() => {
    void Promise.all([api.aiChatThreads(), api.aiModels()]).then(([items, options]) => {
      setThreads(items); setThread(items[0] || null); setModels(options.models); setModel(options.models[0] || '')
    }).catch((reason) => setError(reason instanceof Error ? reason.message : 'AI 对话初始化失败')).finally(() => setLoading(false))
  }, [])
  useEffect(() => { if (thread) void api.aiChatMessages(thread.thread_id).then(setMessages).catch((reason) => setError(reason instanceof Error ? reason.message : '消息加载失败')); else setMessages([]) }, [thread?.thread_id])

  async function createThread() {
    const created = await api.createAIChatThread()
    setThreads((current) => [created, ...current]); setThread(created); setMessages([])
  }
  async function send() {
    const content = draft.trim()
    if (!content || !model || busy) return
    let activeThread = thread
    if (!activeThread) { activeThread = await api.createAIChatThread(); setThreads((current) => [activeThread!, ...current]); setThread(activeThread) }
    const optimistic: AIChatMessage = { message_id: `local-${Date.now()}`, thread_id: activeThread.thread_id, role: 'user', content, mentioned_symbols: [], sources: [], cache_hit: false, input_tokens: 0, output_tokens: 0, created_at: new Date().toISOString() }
    setMessages((current) => [...current, optimistic]); setDraft(''); setStreamed(''); setBusy(true); setError('')
    const controller = new AbortController(); abort.current = controller
    try {
      await streamAIChat(activeThread.thread_id, { content, model, reasoning_effort: effort, web_search: webSearch }, (event) => {
        if (event.type === 'delta') setStreamed((current) => current + String(event.delta || ''))
      }, controller.signal)
      setMessages(await api.aiChatMessages(activeThread.thread_id)); setStreamed(''); setThreads(await api.aiChatThreads())
    } catch (reason) { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : 'AI 对话失败') }
    finally { setBusy(false); abort.current = null }
  }
  const mentions = [...new Set([...positions.map((item) => item.symbol), ...watchlist])].slice(0, 12)
  if (loading) return <Loading label="正在加载 AI 对话" />
  return <div className="ai-chat-layout">
    <Panel title="对话记录" eyebrow="SAVED CHATS" action={<button className="secondary" onClick={() => void createThread()}>新对话</button>}>
      {threads.length ? <div className="chat-thread-list">{threads.map((item) => <button key={item.thread_id} className={thread?.thread_id === item.thread_id ? 'active' : ''} onClick={() => setThread(item)}><strong>{item.title}</strong><small>{formatTime(item.updated_at)}</small></button>)}</div> : <Empty title="暂无对话" />}
    </Panel>
    <Panel title="AI 股票问答" eyebrow="STREAMING RESEARCH CHAT">
      <div className="chat-controls"><label>模型<select value={model} onChange={(event) => setModel(event.target.value)}>{models.map((item) => <option key={item}>{item}</option>)}</select></label><label>思考强度<select value={effort} onChange={(event) => setEffort(event.target.value)}><option value="low">低</option><option value="medium">中</option><option value="high">高</option><option value="xhigh">超高</option></select></label><label className="toggle-line"><input type="checkbox" checked={webSearch} onChange={(event) => setWebSearch(event.target.checked)} />SearXNG 联网</label></div>
      <ErrorNotice message={error} />
      <div className="chat-messages">{messages.map((item) => <article key={item.message_id} className={item.role}><header><strong>{item.role === 'user' ? '你' : 'AI 研究助手'}</strong><small>{item.cache_hit ? '缓存命中 · ' : ''}{formatTime(item.created_at)}</small></header><p>{item.content}</p>{item.sources.length > 0 && <details><summary>查看 {item.sources.length} 个数据来源</summary><ul>{item.sources.map((source, index) => <li key={index}>{typeof source.uri === 'string' && source.uri.startsWith('http') ? <a href={source.uri} target="_blank" rel="noreferrer">{String(source.title || source.uri)}</a> : String(source.symbol || source.source || '系统数据')}</li>)}</ul></details>}</article>)}{streamed && <article className="assistant streaming"><header><strong>AI 研究助手</strong><small>正在生成…</small></header><p>{streamed}<i className="stream-cursor" /></p></article>}</div>
      <div className="mention-strip"><span>@ 股票：</span>{mentions.map((symbol) => <button key={symbol} onClick={() => setDraft((current) => `${current}${current ? ' ' : ''}@${symbol} `)}>{symbol}</button>)}</div>
      <div className="chat-composer"><textarea rows={4} value={draft} placeholder="例如：@600690.SH 结合我的持仓、最新研究和联网信息分析后续风险" onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); void send() } }} /><div><small>联网查询词与来源会随消息审计保存；不会把密码或令牌发送给 SearXNG。</small>{busy ? <button className="secondary" onClick={() => abort.current?.abort()}>停止生成</button> : <button className="primary" disabled={!draft.trim() || !model} onClick={() => void send()}>发送</button>}</div></div>
    </Panel>
  </div>
}
