import { useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api, streamAIChat } from '../api'
import { Empty, ErrorNotice, formatTime, Loading, Panel } from '../components/Ui'
import { useMarket } from '../context/MarketContext'
import type { AIChatAttachment, AIChatMessage, AIChatThread, AICostSummary } from '../types'

type LiveReply = { messageId: string; content: string; status: 'PENDING' | 'STREAMING' | 'FAILED' | 'CANCELLED' }
type ChatStage = 'retrieval' | 'market' | 'news' | 'generation'
type StageState = { status: string; cacheHit?: boolean }
type MentionOption = { symbol: string; name: string }
type MentionMatch = { query: string; start: number; end: number }

const QUICK_QUESTIONS = [
  ['解读最新系统研究报告', '请解读最新系统研究报告，概括市场状态、候选概览和风险结论。'],
  ['生成个股省流版', '请生成 @股票名称或代码 的省流版，并说明是否适合继续查看模拟方案。'],
  ['分析持仓风险', '请结合我的持仓、最新系统研究和风险结论，分析当前持仓风险。'],
  ['比较候选股票', '请比较 @股票A 和 @股票B 的最新系统研究结论、门禁和主要风险。'],
] as const

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

function safeHref(value?: string) {
  if (!value) return null
  if (value.startsWith('/')) return value
  try {
    const parsed = new URL(value)
    return parsed.protocol === 'https:' || parsed.protocol === 'http:' ? parsed.href : null
  } catch { return null }
}

function money(value: number | string | undefined) {
  const amount = Number(value || 0)
  return `$${amount.toFixed(6)}`
}

function currentMention(value: string, caret: number): MentionMatch | null {
  const beforeCaret = value.slice(0, caret)
  const match = /(?:^|\s)@([^\s@]*)$/u.exec(beforeCaret)
  if (!match) return null
  const query = match[1]
  return { query, start: caret - query.length - 1, end: caret }
}

function mentionCandidates(options: MentionOption[], query: string) {
  const normalized = query.trim().toLocaleLowerCase('zh-CN')
  const score = (item: MentionOption) => {
    const name = item.name.toLocaleLowerCase('zh-CN')
    const symbol = item.symbol.toLocaleLowerCase('zh-CN')
    if (!normalized) return 0
    if (name === normalized || symbol === normalized) return 0
    if (name.startsWith(normalized) || symbol.startsWith(normalized)) return 1
    if (name.includes(normalized) || symbol.includes(normalized)) return 2
    return 3
  }
  return options
    .map((item, index) => ({ item, index, score: score(item) }))
    .filter((entry) => entry.score < 3)
    .sort((left, right) => left.score - right.score || left.index - right.index || left.item.symbol.localeCompare(right.item.symbol))
    .slice(0, 6)
    .map((entry) => entry.item)
}

export function SafeMarkdown({ content }: { content: string }) {
  return <ReactMarkdown
    remarkPlugins={[remarkGfm]}
    components={{
      a: ({ href, children }) => {
        const safe = safeHref(href)
        return safe ? <a href={safe} target="_blank" rel="noreferrer noopener">{children}</a> : <span>{children}</span>
      },
      img: () => null,
    }}
  >{content}</ReactMarkdown>
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
  const [mentionCaret, setMentionCaret] = useState(0)
  const [mentionMenuOpen, setMentionMenuOpen] = useState(false)
  const [activeMentionIndex, setActiveMentionIndex] = useState(0)
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
  const composer = useRef<HTMLTextAreaElement>(null)
  const messagesEnd = useRef<HTMLDivElement>(null)
  const queue = useRef<string[]>([])
  const liveContent = useRef('')
  const animation = useRef<number | null>(null)

  const mentionOptions = useMemo(() => {
    const values = new Map<string, string>()
    positions.forEach((item) => values.set(item.symbol, item.name || quotes[item.symbol]?.name || item.symbol))
    watchlist.forEach((symbol) => values.set(symbol, quotes[symbol]?.name || positions.find((item) => item.symbol === symbol)?.name || symbol))
    return Array.from(values, ([symbol, name]) => ({ symbol, name }))
  }, [positions, watchlist, quotes])
  const activeMention = useMemo(() => currentMention(draft, mentionCaret), [draft, mentionCaret])
  const matchedMentionOptions = useMemo(
    () => mentionMenuOpen && activeMention ? mentionCandidates(mentionOptions, activeMention.query) : [],
    [activeMention, mentionMenuOpen, mentionOptions],
  )
  const [stages, setStages] = useState<Partial<Record<ChatStage, StageState>>>({})
  const [streamingMode, setStreamingMode] = useState<'STREAMING' | 'DEGRADED' | 'CACHED' | null>(null)
  const [dataStatus, setDataStatus] = useState<Record<string, unknown> | null>(null)
  const [costSummary, setCostSummary] = useState<AICostSummary | null>(null)

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
    setAttachments([]); setLiveReply(null); setStages({}); setStreamingMode(null); setDataStatus(null)
    if (thread) {
      void api.aiChatMessages(thread.thread_id).then(setMessages).catch((reason) => setError(reason instanceof Error ? reason.message : '消息加载失败'))
      void api.aiCostSummary({ days: 30, threadId: thread.thread_id }).then(setCostSummary).catch(() => setCostSummary(null))
    } else { setMessages([]); setCostSummary(null) }
  }, [thread?.thread_id])
  useEffect(() => { messagesEnd.current?.scrollIntoView({ block: 'end', behavior: 'smooth' }) }, [messages, liveReply?.content])
  useEffect(() => () => { if (animation.current !== null) cancelAnimationFrame(animation.current) }, [])
  useEffect(() => { setActiveMentionIndex(0) }, [activeMention?.query, matchedMentionOptions.length])

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

  function insertMention(item: MentionOption) {
    if (!activeMention) return
    const next = `${draft.slice(0, activeMention.start)}@${item.name} ${draft.slice(activeMention.end)}`
    const nextCaret = activeMention.start + item.name.length + 2
    setDraft(next)
    setMentionCaret(nextCaret)
    setMentionMenuOpen(false)
    window.requestAnimationFrame(() => {
      composer.current?.focus()
      composer.current?.setSelectionRange(nextCaret, nextCaret)
    })
  }

  function useQuickQuestion(value: string) {
    setDraft(value); setMentionCaret(value.length); setMentionMenuOpen(false)
    window.requestAnimationFrame(() => {
      composer.current?.focus()
      composer.current?.setSelectionRange(value.length, value.length)
    })
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
    liveContent.current = ''; queue.current = []; setLiveReply({ messageId: localAssistantId, content: '', status: 'PENDING' }); setStages({}); setStreamingMode(null); setDataStatus(null)
    const usedAttachmentIds = attachments.map((item) => item.attachment_id); setAttachments([])
    const controller = new AbortController(); abort.current = controller
    try {
      await streamAIChat(activeThread.thread_id, { content, model, reasoning_effort: effort, web_search: webSearch, attachment_ids: usedAttachmentIds, mention_refs: mentionRefs }, (event) => {
        if (event.type === 'meta') {
          if (event.assistant_message_id) setLiveReply((current) => current ? { ...current, messageId: String(event.assistant_message_id) } : current)
          if (event.data_status && typeof event.data_status === 'object') setDataStatus(event.data_status as Record<string, unknown>)
        }
        if (event.type === 'stage' && typeof event.stage === 'string') {
          const stage = event.stage as ChatStage
          setStages((current) => ({ ...current, [stage]: { status: String(event.status || 'STARTED'), cacheHit: Boolean(event.cache_hit) } }))
          if (event.status === 'DEGRADED') setStreamingMode('DEGRADED')
        }
        if (event.type === 'delta') enqueueDelta(String(event.delta || ''))
        if (event.type === 'done') { flushQueue(); if (typeof event.streaming_mode === 'string') setStreamingMode(event.streaming_mode as 'STREAMING' | 'DEGRADED' | 'CACHED') }
      }, controller.signal, crypto.randomUUID())
      flushQueue(); setMessages(await api.aiChatMessages(activeThread.thread_id)); void api.aiCostSummary({ days: 30, threadId: activeThread.thread_id }).then(setCostSummary).catch(() => setCostSummary(null)); setLiveReply(null); await loadThreads(activeThread.thread_id)
    } catch (reason) {
      flushQueue()
      const cancelled = controller.signal.aborted
      const partial: AIChatMessage = { message_id: liveReply?.messageId || localAssistantId, thread_id: activeThread.thread_id, role: 'assistant', content: liveContent.current, status: cancelled ? 'CANCELLED' : 'FAILED', mentioned_symbols: mentionRefs.map((item) => item.symbol), mention_refs: mentionRefs, attachment_ids: [], sources: [], cache_hit: false, input_tokens: 0, output_tokens: 0, created_at: now }
      setMessages((current) => [...current, partial]); setLiveReply(null)
      if (!cancelled) setError(reason instanceof Error ? reason.message : 'AI 对话失败')
    } finally { setBusy(false); abort.current = null }
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
      {(busy || Object.keys(stages).length > 0) && <div className="chat-stage-strip" aria-live="polite">{(['retrieval', 'market', 'news', 'generation'] as ChatStage[]).map((stage) => <span className={`chat-stage ${stages[stage]?.status?.toLowerCase() || 'waiting'}`} key={stage}>{({ retrieval: '检索', market: '行情', news: '新闻', generation: '生成' } as Record<ChatStage, string>)[stage]}：{stages[stage]?.status === 'DEGRADED' ? '已降级' : stages[stage]?.status === 'CACHED' ? '缓存' : stages[stage]?.status === 'COMPLETED' ? '完成' : stages[stage]?.status === 'STARTED' ? '进行中' : '等待'}{stages[stage]?.cacheHit ? ' · 命中' : ''}</span>)}</div>}
      {streamingMode === 'DEGRADED' && <div className="warning-box chat-degraded"><strong>流式已降级</strong><p>当前模型网关返回一次性结果；本次状态已记录并通知管理员。</p></div>}
      {dataStatus && <details className="chat-data-status"><summary>查看本次上下文数据状态</summary><pre>{JSON.stringify(dataStatus, null, 2)}</pre></details>}
      {costSummary && <section className="chat-cost-panel" aria-label="AI 成本摘要"><div className="chat-cost-heading"><strong>本轮与近 30 天成本</strong><small>估算金额以当前模型档案单价为准</small></div><div className="chat-cost-grid"><div><span>本轮支出</span><strong>{money(costSummary.current_turn?.estimated_spend_usd)}</strong><small>节省 {money(costSummary.current_turn?.estimated_savings_usd)}</small></div><div><span>近 30 天支出</span><strong>{money(costSummary.totals.estimated_spend_usd)}</strong><small>节省 {money(costSummary.totals.estimated_savings_usd)}</small></div><div><span>缓存读取 / 写入</span><strong>{costSummary.totals.cached_input_tokens.toLocaleString()} / {costSummary.totals.cache_write_tokens.toLocaleString()}</strong><small>未缓存输入 {costSummary.totals.uncached_input_tokens.toLocaleString()}</small></div><div><span>缓存命中率</span><strong>{costSummary.totals.requests ? `${(costSummary.totals.cache_hits / costSummary.totals.requests * 100).toFixed(1)}%` : '0.0%'}</strong><small>{costSummary.totals.cache_hits} / {costSummary.totals.requests} 次</small></div></div></section>}
      <div className="chat-messages">{messages.map((item) => <article key={item.message_id} className={`${item.role} ${item.status?.toLowerCase() || ''}`}><header><strong>{item.role === 'user' ? '你' : 'AI 研究助手'}</strong><small>{item.cache_hit ? '缓存命中 · ' : ''}{item.streaming_mode === 'DEGRADED' ? '一次性回复 · ' : ''}{item.status && !['COMPLETED'].includes(item.status) ? `${item.status === 'CANCELLED' ? '未完成' : '回复失败'} · ` : ''}{formatTime(item.created_at)}</small></header><div className="markdown-content"><SafeMarkdown content={item.content} /></div>{Boolean(item.attachment_ids?.length) && <div className="message-images">{item.attachment_ids?.map((id) => <AttachmentImage id={id} key={id} />)}</div>}{item.sources.length > 0 && <details><summary>查看 {item.sources.length} 个数据来源</summary><ul>{item.sources.map((source, index) => { const href = typeof source.uri === 'string' ? safeHref(source.uri) : null; return <li key={index}>{href ? <a href={href} target="_blank" rel="noreferrer noopener">{String(source.title || source.uri)}</a> : String(source.symbol || source.source || '系统数据')}</li> })}</ul></details>}</article>)}{liveReply && <article className={`assistant streaming ${liveReply.status.toLowerCase()}`}><header><strong>AI 研究助手</strong><small>{liveReply.status === 'PENDING' ? 'AI 正在回复…' : '正在生成…'}</small></header><div className="markdown-content"><SafeMarkdown content={liveReply.content} /></div><i className="stream-cursor" /></article>}<div ref={messagesEnd} /></div>
      {attachments.length > 0 && <div className="pending-images">{attachments.map((item) => <div key={item.attachment_id}><AttachmentImage id={item.attachment_id} /><button aria-label="移除图片" onClick={() => setAttachments((current) => current.filter((entry) => entry.attachment_id !== item.attachment_id))}>×</button><small>{formatTime(item.expires_at)} 销毁</small></div>)}</div>}
      <div className="image-retention-warning">图片将在上传 7 天后自动销毁，请自行保存原图。到期后历史对话仅保留占位和已有 AI 分析。</div>
      <div className="chat-composer"><div className="quick-questions" aria-label="快捷问题">{QUICK_QUESTIONS.map(([label, value]) => <button className="secondary" type="button" key={label} onClick={() => useQuickQuestion(value)}>{label}</button>)}</div><div className="mention-composer"><textarea ref={composer} rows={4} value={draft} placeholder="例如：输入 @ 后继续输入股票名称，结合我的持仓、最新研究和联网信息分析后续风险" onChange={(event) => { setDraft(event.target.value); setMentionCaret(event.target.selectionStart); setMentionMenuOpen(true) }} onSelect={(event) => setMentionCaret(event.currentTarget.selectionStart)} onKeyDown={(event) => {
        if (matchedMentionOptions.length) {
          if (event.key === 'ArrowDown') { event.preventDefault(); setActiveMentionIndex((index) => (index + 1) % matchedMentionOptions.length); return }
          if (event.key === 'ArrowUp') { event.preventDefault(); setActiveMentionIndex((index) => (index - 1 + matchedMentionOptions.length) % matchedMentionOptions.length); return }
          if (event.key === 'Escape') { event.preventDefault(); setMentionMenuOpen(false); return }
          if (event.key === 'Enter' || event.key === 'Tab') { event.preventDefault(); insertMention(matchedMentionOptions[activeMentionIndex]); return }
        }
        if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); void send() }
      }} />
        {matchedMentionOptions.length > 0 && <div className="mention-menu" role="listbox" aria-label="匹配的股票">{matchedMentionOptions.map((item, index) => <button className={index === activeMentionIndex ? 'active' : ''} type="button" role="option" aria-selected={index === activeMentionIndex} key={item.symbol} onMouseDown={(event) => event.preventDefault()} onClick={() => insertMention(item)}><span><strong>{item.name}</strong><small>{item.symbol}</small></span><em>{index === 0 ? '最佳匹配' : '自选 / 持仓'}</em></button>)}</div>}
      </div><small className="mention-help">输入 <code>@</code> 后按名称或代码搜索；候选优先来自持仓和自选。也可直接输入任意已收录 A 股的 <code>@6位代码</code> 或标准代码。</small><div><span><input ref={fileInput} hidden type="file" multiple accept="image/png,image/jpeg,image/webp,image/gif" onChange={(event) => void selectFiles(Array.from(event.target.files || []))} /><button className="secondary" disabled={busy || uploading || attachments.length >= 4} onClick={() => fileInput.current?.click()}>{uploading ? '上传中…' : '添加图片'}</button></span>{busy ? <button className="secondary" onClick={() => abort.current?.abort()}>停止生成</button> : <button className="primary" disabled={(!draft.trim() && !attachments.length) || !model || uploading} onClick={() => void send()}>发送</button>}</div></div>
    </Panel>
  </div>
}
