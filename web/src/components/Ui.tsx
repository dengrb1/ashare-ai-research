import type { ReactNode } from 'react'
import { Inbox } from 'lucide-react'
import { ErrorToast } from '../context/ToastContext'

export function AsharePriceText({ value, digits = 2, className = '' }: { value?: number | null; digits?: number; className?: string }) {
  return <span className={`tabular-nums metric-number price-flat ${className}`}>{value === undefined || value === null || Number.isNaN(value) ? '—' : formatNumber(value, digits)}</span>
}

export function AshareDeltaPill({ value, suffix = '%' }: { value?: number | null; suffix?: string }) {
  if (value === undefined || value === null || Number.isNaN(value)) return <span className="tabular-nums price-flat">—</span>
  const flat = Math.abs(value) < 1e-6
  const up = value > 0 && !flat
  const tone = flat ? 'price-flat' : up ? 'price-up' : 'price-down'
  return <span className={`tabular-nums delta-pill ${tone}`}>{up ? '+' : ''}{value.toFixed(2)}{suffix}</span>
}

export function ConnectionCapsule({ state, source, reconnectAttempt = 0, onRetry }: { state: 'CONNECTED' | 'RECONNECTING' | 'DISCONNECTED'; source?: string; reconnectAttempt?: number; onRetry?: () => void }) {
  const config = {
    CONNECTED: { dot: 'var(--ashare-up)', label: `行情已连接 · ${source || '实时流'}` },
    RECONNECTING: { dot: 'var(--accent-amber)', label: `正在重连 (${reconnectAttempt})` },
    DISCONNECTED: { dot: 'var(--ashare-down)', label: '行情已断开' },
  }[state]
  return <div className="connection-capsule tabular-nums" role="status"><i style={{ backgroundColor: config.dot, boxShadow: `0 0 8px ${config.dot}` }} /><span>{config.label}</span>{state === 'DISCONNECTED' && onRetry && <button type="button" className="secondary" onClick={onRetry}>重试</button>}</div>
}

export function Panel({ title, eyebrow, action, children, className = '' }: { title?: string; eyebrow?: string; action?: ReactNode; children: ReactNode; className?: string }) {
  return <section className={`panel ${className}`}>
    {(title || eyebrow || action) && <header className="panel-header">
      <div>{eyebrow && <div className="eyebrow">{eyebrow}</div>}{title && <h2>{title}</h2>}</div>
      {action && <div className="panel-action">{action}</div>}
    </header>}
    {children}
  </section>
}

export function StatusPill({ status }: { status: string }) {
  const normalized = status.toUpperCase()
  const tone = ['SUCCESS', 'COMPLETED', 'SUCCEEDED', 'ACTIVE'].includes(normalized) ? 'success'
    : normalized === 'FUSED' ? 'warning'
    : ['FAILED', 'ERROR', 'DISABLED', 'CANCELLED'].includes(normalized) ? 'danger'
      : ['RUNNING', 'PROCESSING'].includes(normalized) ? 'running' : 'pending'
  const labels: Record<string, string> = { SUCCESS: '已完成', COMPLETED: '已完成', SUCCEEDED: '已完成', FUSED: '观察模式', FAILED: '失败', ERROR: '异常', RUNNING: '运行中', PROCESSING: '处理中', PENDING: '等待中', QUEUED: '排队中', CANCEL_REQUESTED: '正在停止', CANCELLED: '已停止', ACTIVE: '启用', DISABLED: '禁用' }
  return <span className={`status-pill ${tone}`}><i />{labels[normalized] || status}</span>
}

export function Empty({ title = '暂无数据', description }: { title?: string; description?: string }) {
  return <div className="empty"><span><Inbox size={28} strokeWidth={1.4} /></span><strong>{title}</strong>{description && <p>{description}</p>}</div>
}

export function Loading({ label = '加载中' }: { label?: string }) {
  return <div className="loading"><i />{label}</div>
}

export function ErrorNotice({ message }: { message?: string }) {
  return <ErrorToast message={message} />
}

export function formatNumber(value?: number | null, digits = 2) {
  if (value === undefined || value === null || Number.isNaN(value)) return '—'
  return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: digits }).format(value)
}

export function formatAmount(value?: number) {
  if (value === undefined || value === null) return '—'
  if (Math.abs(value) >= 1e8) return `${(value / 1e8).toFixed(2)}亿`
  if (Math.abs(value) >= 1e4) return `${(value / 1e4).toFixed(1)}万`
  return formatNumber(value, 0)
}

export function formatTime(value?: string) {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
}

export function today() {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(new Date())
  const value = Object.fromEntries(parts.map((part) => [part.type, part.value]))
  return `${value.year}-${value.month}-${value.day}`
}
