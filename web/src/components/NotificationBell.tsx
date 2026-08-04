import { Bell, CheckCheck, Trash2, X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { formatTime } from './Ui'
import type { Notification, NotificationSummary } from '../types'

const EMPTY_SUMMARY: NotificationSummary = { unread_count: 0, high_risk_unread_count: 0, latest: [] }

function severityLabel(item: Notification) {
  return item.severity === 'CRITICAL' ? '高风险' : item.severity === 'HIGH' ? '重要' : item.severity === 'WARNING' ? '提醒' : '通知'
}

export function NotificationBell() {
  const [summary, setSummary] = useState<NotificationSummary>(EMPTY_SUMMARY)
  const [items, setItems] = useState<Notification[]>([])
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const panel = useRef<HTMLDivElement>(null)

  async function refreshSummary() {
    if (document.visibilityState !== 'visible') return
    try { setSummary(await api.notificationSummary()) } catch { /* A notification failure must not block the shell. */ }
  }

  async function loadItems(cursor?: string, append = false) {
    setLoading(true)
    try {
      const payload = await api.notifications({ cursor, limit: 30 })
      setItems((current) => append ? [...current, ...payload.items] : payload.items)
      setNextCursor(payload.next_cursor || null)
    } finally { setLoading(false) }
  }

  useEffect(() => {
    void refreshSummary()
    const timer = window.setInterval(() => void refreshSummary(), 30_000)
    const onVisibility = () => void refreshSummary()
    document.addEventListener('visibilitychange', onVisibility)
    return () => { window.clearInterval(timer); document.removeEventListener('visibilitychange', onVisibility) }
  }, [])

  useEffect(() => {
    if (!open) return
    void loadItems()
    const onPointerDown = (event: MouseEvent) => {
      if (panel.current && !panel.current.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onPointerDown)
    return () => document.removeEventListener('mousedown', onPointerDown)
  }, [open])

  async function markRead(id: string) {
    const updated = await api.markNotificationsRead([id])
    setSummary(updated)
    setItems((current) => current.map((item) => item.notification_id === id ? { ...item, read_at: new Date().toISOString() } : item))
  }

  async function markAllRead() {
    const updated = await api.markAllNotificationsRead()
    setSummary(updated)
    setItems((current) => current.map((item) => ({ ...item, read_at: item.read_at || new Date().toISOString() })))
  }

  async function removeOne(id: string) {
    try {
      await api.deleteNotification(id)
      setItems((current) => current.filter((item) => item.notification_id !== id))
      setSummary(await api.notificationSummary())
    } catch { /* A failed delete keeps the notification visible. */ }
  }

  async function clearAll() {
    if (!window.confirm('确定清空全部通知？此操作不可恢复。')) return
    try {
      const updated = await api.clearNotifications()
      setSummary(updated)
      setItems([])
      setNextCursor(null)
    } catch { /* A failed clear keeps the current items visible. */ }
  }

  return <div className="notification-center" ref={panel}>
    <button className={`notification-bell ${summary.high_risk_unread_count ? 'risk' : ''}`} onClick={() => setOpen((current) => !current)} aria-label={`通知中心，${summary.unread_count} 条未读`} aria-expanded={open} title="通知中心">
      <Bell size={19} strokeWidth={1.9} aria-hidden="true" />
      {summary.unread_count > 0 && <span className="notification-badge">{summary.unread_count > 99 ? '99+' : summary.unread_count}</span>}
    </button>
    {open && <section className="notification-panel" aria-label="通知中心">
      <header><div><strong>通知中心</strong><small>{summary.unread_count ? `${summary.unread_count} 条未读` : '暂无未读通知'}</small></div><div><button className="icon-button" disabled={!summary.unread_count} onClick={() => void markAllRead()} aria-label="全部标为已读" title="全部标为已读"><CheckCheck size={17} /></button><button className="icon-button" disabled={!items.length} onClick={() => void clearAll()} aria-label="清空全部通知" title="清空全部通知"><Trash2 size={16} /></button><button className="icon-button" onClick={() => setOpen(false)} aria-label="关闭通知中心" title="关闭"><X size={17} /></button></div></header>
      <div className="notification-list">
        {items.length === 0 && !loading && <p className="notification-empty">暂无通知</p>}
        {items.map((item) => <article className={`notification-item ${item.read_at ? 'read' : 'unread'} ${item.severity.toLowerCase()}`} key={item.notification_id}>
          <div><span className="notification-severity">{severityLabel(item)}</span><strong>{item.title}</strong><p>{item.body}</p><small>{formatTime(item.created_at)}</small></div>
          <div className="notification-actions">
            {!item.read_at && <button className="icon-button notification-read" onClick={() => void markRead(item.notification_id)} aria-label={`标记“${item.title}”为已读`} title="标为已读"><CheckCheck size={16} /></button>}
            <button className="icon-button notification-delete" onClick={() => void removeOne(item.notification_id)} aria-label={`删除“${item.title}”`} title="删除"><Trash2 size={15} /></button>
          </div>
        </article>)}
      </div>
      {nextCursor && <button className="secondary notification-more" disabled={loading} onClick={() => void loadItems(nextCursor, true)}>{loading ? '加载中…' : '加载更多'}</button>}
    </section>}
  </div>
}
