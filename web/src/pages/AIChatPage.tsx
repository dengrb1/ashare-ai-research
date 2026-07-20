import { useEffect, useMemo, useRef, useState } from 'react'
import { api, streamAIChat } from '../api'
import { Empty, ErrorNotice, formatTime, Loading, Panel } from '../components/Ui'
import { useMarket } from '../context/MarketContext'
import type { AIChatAttachment, AIChatMessage, AIChatThread } from '../types'

type LiveReply = { messageId: string; content: string; status: 'PENDING' | 'STREAMING' | 'FAILED' | 'CANCELLED' }

function splitGraphemes(value: string) {
  if (typeof Intl.Segmenter === 'function') {
    return Array.from(new Intl.Segmenter('zh-CN', { granularity: 'grapheme' }).segment(value), (item) => item.segment)
  }
  return Array.from(value)
}

function AttachmentImage({ id }: { id: string }) {
  const [failed, setFailed] = useState(false)
  if (failed) return <div className="expired-image">图片已按七天保留策略销毁</div>
  return <img src={`/api/v1/ai/chat/attachments/${encodeURIComponent(id)}/content`} alt="对话附图" loading="lazy" onError={() => setFailed(true)} />
}

export function AIChatPage() {
  const { positions, watchlist, quotes } = useMarket()
  const [threads, setThreads] = useState<AIChatThread[]>([])
  const [thread, setThread] = useState<AIChatThread | null>(null)
  const [messages, setMessages] = useState<AIChatMessage[]>([])
  const [models, setModels] = useState<string[]>([])
  const [model, setModel] = useState('')
  const [effort, setEffort] = useState('medium')
  const [webSearch, setWebSearch] = useState(true)
  const [draft, setDraft] = useState('')
  const [liveReply, setLiveReply] = useState<LiveReply | null>(null)
  const [attachments, setAttachments] = useState<AIChatAttachment[]>([])
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState('')
  const [search, setSearch] = useState('')
  const [showArchived, setShowArchived] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const abort = useRef<AbortController | null>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const messagesEnd = useRef<HTMLDivElement>(null)
  const queue = useRef<string[]>([])
  const liveContent = useRef('')
  const animation = useRef<number | null>(null)

  const mentionOptions = useMemo(() => {
    const values = new Map<string, string>()
    positions.forEach((item) => values.set(item.symbol, item.name || quotes[item.symbol]?.name || item.symbol))
    watchlist.forEach((symbol) => values.set(symbol, quotes[symbol]?.name || positions.find((item) => item.symbol === symbol)?.name || symbol))
    return Array.from(values, ([symbol, name]) => ({ symbol, name })).slice(0, 20)
  }, [positions, watchlist, quotes])
  const nameBySymbol = useMemo(() => new Map(mentionOptions.map((item) => [item.symbol, item.name])), [mentionOptions])

  async function loadThreads(preferredId?: string) {
    const payload = await api.aiChatThreadIndex({ q: search || undefined, archived: showArchived, limit: 100 })
    setThreads(payload.items)
    setThread((current) => payload.items.find((item) => item.thread_id === (preferredId || current?.thread_id)) || payload.items[0] || null)
  }

  useEffect(() => {
    void api.aiModels().then((options) => { setModels(options.models); setModel(options.models[0] || '') }).catch((reason) => setError(reason instanceof Error ? reason.message : '模型列表加载失败'))
  }, [])
  useEffect(() => {
    const timer = window.setTimeout(() => void loadThreads().catch((reason) => setError(reason instanceof Error ? reason.message : '对话列表加载失败')).finally(() => setLoading(false)), 180)
    return () => window.clearTimeout(timer)
  }, [search, showArchived])
  useEffect(() => {
    setAttachments([]); setLiveReply(null)
    if (thread) void api.aiChatMessages(thread.thread_id).then(setMessages).catch((reason) => setError(reason instanceof Error ? reason.message : '消息加载失败'))
    else setMessages([])
  }, [thread?.thread_id])
  useEffect(() => { messagesEnd.current?.scrollIntoView({ block: 'end', behavior: 'smooth' }) }, [messages, liveReply?.content])
  useEffect(() => () => { if (animation.current !== null) cancelAnimationFrame(animation.current) }, [])

  function tickQueue() {
    const take = Math.max(1, Math.min(4, Math.ceil(queue.current.length / 24)))
    liveContent.current += queue.current.splice(0, take).join('')
    setLiveReply((current) => current ? { ...current, content: liveContent.current, status: 'STREAMING' } : current)
    animation.current = queue.current.length ? requestAnimationFrame(tickQueue) : null
  }
  function enqueueDelta(delta: string) {
    queue.current.push(...splitGraphemes(delta))
    if (animation.current === null) animation.current = requestAnimationFrame(tickQueue)
  }
  function flushQueue() {
    if (animation.current !== null) cancelAnimationFrame(animation.current)
    animation.current = null
    liveContent.current += queue.current.splice(0).join('')
    setLiveReply((current) => current ? { ...current, content: liveContent.current, status: 'STREAMING' } : current)
  }

  async function createThread() {
    const created = await api.createAIChatThread()
    await loadThreads(created.thread_id); setThread(created); setMessages([]); setAttachments([])
    return created
  }
  async function patchThread(item: AIChatThread, payload: Parameters<typeof api.patchAIChatThread>[1]) {
    const updated = await api.patchAIChatThread(item.thread_id, payload)
    await loadThreads(updated.thread_id)
  }
  async function renameThread(item: AIChatThread) {
    const title = window.prompt('输入新对话名称', item.title)?.trim()
    if (title) await patchThread(item, { title })
  }
  async function regroupThread(item: AIChatThread) {
    const label = window.prompt('输入手动分组名称；留空恢复自动分组', item.group_mode === 'MANUAL' ? item.group_label || '' : '')
    if (label !== null) await patchThread(item, { group_label: label.trim() || null })
  }
  async function deleteThread(item: AIChatThread) {
    if (!window.confirm(`删除“${item.title}”及其仍在保留期的图片？`)) return
    await api.deleteAIChatThread(item.thread_id); setSelected((current) => { const next = new Set(current); next.delete(item.thread_id); return next }); await loadThreads()
  }
  async function bulkDelete() {
    const ids = Array.from(selected)
    if (!ids.length || !window.confirm(`删除已选的 ${ids.length} 个对话？`)) return
    await api.bulkDeleteAIChatThreads(ids); setSelected(new Set()); await loadThreads()
  }

  async function selectFiles(files: File[]) {
    if (!files.length) return
    if (attachments.length + files.length > 4) { setError('每条消息最多 4 张图片'); return }
    const total = [...files].reduce((sum, file) => sum + file.size, 0) + attachments.reduce((sum, item) => sum + item.byte_size, 0)
    if (files.some((file) => file.size > 10 * 1024 * 1024) || total > 25 * 1024 * 1024) { setError('单张不超过 10 MB，每条消息合计不超过 25 MB'); return }
    setUploading(true); setError('')
    try {
      const active = thread || await createThread()
      const uploaded = await api.uploadAIChatAttachments(files, active.thread_id)
      setAttachments((current) => [...current, ...uploaded])
    } catch (reason) { setError(reason instanceof Error ? reason.message : '图片上传失败') }
    finally { setUploading(false); if (fileInput.current) fileInput.current.value = '' }
  }

  async function send() {
    const content = draft.trim() || (attachments.length ? '请分析这些图片' : '')
    if (!content || !model || busy) return
    let activeThread = thread
    if (!activeThread) activeThread = await createThread()
    const now = new Date().toISOString()
    const mentionRefs = mentionOptions.filter((item) => content.includes(`@${item.name}`) || content.toUpperCase().includes(`@${item.symbol}`))
    const optimisticUser: AIChatMessage = { message_id: `local-user-${Date.now()}`, thread_id: activeThread.thread_id, role: 'user', content, status: 'COMPLETED', mentioned_symbols: mentionRefs.map((item) => item.symbol), mention_refs: mentionRefs, attachment_ids: attachments.map((item) => item.attachment_id), sources: [], cache_hit: false, input_tokens: 0, output_tokens: 0, created_at: now }
    const localAssistantId = `local-assistant-${Date.now()}`
    setMessages((current) => [...current, optimisticUser]); setDraft(''); setBusy(true); setError('')
    liveContent.current = ''; queue.current = []; setLiveReply({ messageId: localAssistantId, content: '', status: 'PENDING' })
    const usedAttachmentIds = attachments.map((item) => item.attachment_id); setAttachments([])
    const controller = new AbortController(); abort.current = controller
    try {
      await streamAIChat(activeThread.thread_id, { content, model, reasoning_effort: effort, web_search: webSearch, attachment_ids: usedAttachmentIds, mention_refs: mentionRefs, decision_at: now }, (event) => {
        if (event.type === 'meta' && event.assistant_message_id) setLiveReply((current) => current ? { ...current, messageId: String(event.assistant_message_id) } : current)
        if (event.type === 'delta') enqueueDelta(String(event.delta || ''))
        if (event.type === 'done') flushQueue()
      }, controller.signal, crypto.randomUUID())
      flushQueue(); setMessages(await api.aiChatMessages(activeThread.thread_id)); setLiveReply(null); await loadThreads(activeThread.thread_id)
    } catch (reason) {
      flushQueue()
      const cancelled = controller.signal.aborted
      const partial: AIChatMessage = { message_id: liveReply?.messageId || localAssistantId, thread_id: activeThread.thread_id, role: 'assistant', content: liveContent.current, status: cancelled ? 'CANCELLED' : 'FAILED', mentioned_symbols: mentionRefs.map((item) => item.symbol), mention_refs: mentionRefs, attachment_ids: [], sources: [], cache_hit: false, input_tokens: 0, output_tokens: 0, created_at: now }
      setMessages((current) => [...current, partial]); setLiveReply(null)
      if (!cancelled) setError(reason instanceof Error ? reason.message : 'AI 对话失败')
    } finally { setBusy(false); abort.current = null }
  }

  function renderContent(item: AIChatMessage) {
    const localNames = new Map(nameBySymbol)
    item.mention_refs?.forEach((ref) => localNames.set(ref.symbol, ref.name))
    return item.content.split(/(@\d{6}\.(?:SH|SZ|BJ))/gi).map((part, index) => {
      const symbol = part.startsWith('@') ? part.slice(1).toUpperCase() : ''
      return symbol && localNames.has(symbol) ? <span className="stock-mention" title={symbol} key={index}>@{localNames.get(symbol)}</span> : part
    })
  }

  if (loading) return <Loading label="正在加载 AI 对话" />
  return <div className="ai-chat-layout">
    <Panel title="对话记录" eyebrow="SAVED CHATS" action={<button className="secondary" onClick={() => void createThread()}>新对话</button>}>
      <div className="thread-tools"><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索对话或分组" /><label className="toggle-line"><input type="checkbox" checked={showArchived} onChange={(event) => setShowArchived(event.target.checked)} />已归档</label></div>
      {selected.size > 0 && <button className="danger-button bulk-delete" onClick={() => void bulkDelete()}>批量删除 {selected.size} 项</button>}
      {threads.length ? <div className="chat-thread-list">{threads.map((item) => <div className={`thread-row ${thread?.thread_id === item.thread_id ? 'active' : ''}`} key={item.thread_id}><input type="checkbox" aria-label={`选择 ${item.title}`} checked={selected.has(item.thread_id)} onChange={(event) => setSelected((current) => { const next = new Set(current); event.target.checked ? next.add(item.thread_id) : next.delete(item.thread_id); return next })} /><button className="thread-main" onClick={() => setThread(item)}><span><strong>{item.pinned_at ? '◆ ' : ''}{item.title}</strong><small>{item.group_label || '综合问答'} · {formatTime(item.updated_at)}</small></span></button><details><summary aria-label="对话操作">⋯</summary><button onClick={() => void renameThread(item)}>重命名</button><button onClick={() => void regroupThread(item)}>调整分组</button><button onClick={() => void patchThread(item, { pinned: !item.pinned_at })}>{item.pinned_at ? '取消置顶' : '置顶'}</button><button onClick={() => void patchThread(item, { archived: !item.archived_at })}>{item.archived_at ? '恢复' : '归档'}</button><button className="danger" onClick={() => void deleteThread(item)}>删除</button></details></div>)}</div> : <Empty title={showArchived ? '暂无已归档对话' : '暂无对话'} />}
    </Panel>
    <Panel title="AI 股票问答" eyebrow="STREAMING RESEARCH CHAT">
      <div className="chat-controls"><label>模型<select value={model} onChange={(event) => setModel(event.target.value)}>{models.map((item) => <option key={item}>{item}</option>)}</select></label><label>思考强度<select value={effort} onChange={(event) => setEffort(event.target.value)}><option value="low">低</option><option value="medium">中</option><option value="high">高</option><option value="xhigh">超高</option></select></label><label className="toggle-line"><input type="checkbox" checked={webSearch} onChange={(event) => setWebSearch(event.target.checked)} />SearXNG 联网</label></div>
      <ErrorNotice message={error} />
      <div className="chat-messages">{messages.map((item) => <article key={item.message_id} className={`${item.role} ${item.status?.toLowerCase() || ''}`}><header><strong>{item.role === 'user' ? '你' : 'AI 研究助手'}</strong><small>{item.cache_hit ? '缓存命中 · ' : ''}{item.status && !['COMPLETED'].includes(item.status) ? `${item.status === 'CANCELLED' ? '未完成' : '回复失败'} · ` : ''}{formatTime(item.created_at)}</small></header><p>{renderContent(item)}</p>{Boolean(item.attachment_ids?.length) && <div className="message-images">{item.attachment_ids?.map((id) => <AttachmentImage id={id} key={id} />)}</div>}{item.sources.length > 0 && <details><summary>查看 {item.sources.length} 个数据来源</summary><ul>{item.sources.map((source, index) => <li key={index}>{typeof source.uri === 'string' && source.uri.startsWith('http') ? <a href={source.uri} target="_blank" rel="noreferrer">{String(source.title || source.uri)}</a> : String(source.symbol || source.source || '系统数据')}</li>)}</ul></details>}</article>)}{liveReply && <article className={`assistant streaming ${liveReply.status.toLowerCase()}`}><header><strong>AI 研究助手</strong><small>{liveReply.status === 'PENDING' ? 'AI 正在回复…' : '正在生成…'}</small></header><p>{liveReply.content}<i className="stream-cursor" /></p></article>}<div ref={messagesEnd} /></div>
      <div className="mention-strip"><span>@ 股票：</span>{mentionOptions.map((item) => <button key={item.symbol} title={item.symbol} onClick={() => setDraft((current) => `${current}${current ? ' ' : ''}@${item.name} `)}>@{item.name}</button>)}</div>
      {attachments.length > 0 && <div className="pending-images">{attachments.map((item) => <div key={item.attachment_id}><AttachmentImage id={item.attachment_id} /><button aria-label="移除图片" onClick={() => setAttachments((current) => current.filter((entry) => entry.attachment_id !== item.attachment_id))}>×</button><small>{formatTime(item.expires_at)} 销毁</small></div>)}</div>}
      <div className="image-retention-warning">图片将在上传 7 天后自动销毁，请自行保存原图。到期后历史对话仅保留占位和已有 AI 分析。</div>
      <div className="chat-composer"><textarea rows={4} value={draft} placeholder="例如：@海尔智家 结合我的持仓、最新研究和联网信息分析后续风险" onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); void send() } }} /><div><span><input ref={fileInput} hidden type="file" multiple accept="image/png,image/jpeg,image/webp,image/gif" onChange={(event) => void selectFiles(Array.from(event.target.files || []))} /><button className="secondary" disabled={busy || uploading || attachments.length >= 4} onClick={() => fileInput.current?.click()}>{uploading ? '上传中…' : '添加图片'}</button></span>{busy ? <button className="secondary" onClick={() => abort.current?.abort()}>停止生成</button> : <button className="primary" disabled={(!draft.trim() && !attachments.length) || !model || uploading} onClick={() => void send()}>发送</button>}</div></div>
    </Panel>
  </div>
}
