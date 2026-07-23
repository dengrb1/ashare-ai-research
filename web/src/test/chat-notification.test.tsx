import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../context/MarketContext', () => ({
  useMarket: () => ({ positions: [{ symbol: '600519.SH', name: '贵州茅台', quantity: 100, cost: 1400 }], watchlist: ['600519.SH', '000001.SZ'], quotes: { '000001.SZ': { name: '平安银行', price: 10, change_pct: 0 } } }),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      aiModels: vi.fn(),
      aiChatMessages: vi.fn(),
      aiChatThreadIndex: vi.fn(),
      notificationSummary: vi.fn(),
      notifications: vi.fn(),
      markAllNotificationsRead: vi.fn(),
      markNotificationsRead: vi.fn(),
    },
    streamAIChat: vi.fn(),
  }
})

import { api, streamAIChat } from '../api'
import { NotificationBell } from '../components/NotificationBell'
import { AIChatPage, SafeMarkdown } from '../pages/AIChatPage'

const thread = {
  thread_id: 'thread-1',
  title: '测试对话',
  created_at: '2026-07-20T00:00:00Z',
  updated_at: '2026-07-20T00:00:00Z',
}

const notification = {
  notification_id: 'notice-1',
  notification_type: 'STOP_LOSS_TRIGGERED',
  severity: 'CRITICAL' as const,
  title: '止损预警',
  body: '仅生成模拟退出研究。',
  payload: {},
  read_at: null,
  created_at: '2026-07-20T00:00:00Z',
  expires_at: '2027-01-16T00:00:00Z',
}

beforeEach(() => {
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
    configurable: true,
    value: vi.fn(),
  })
  vi.mocked(api.aiModels).mockResolvedValue({
    models: ['gpt-test'],
    reasoning_efforts: ['medium'],
    web_search_available: true,
    cache_enabled: true,
  })
  vi.mocked(api.aiChatThreadIndex).mockResolvedValue({ items: [thread], next_cursor: null })
  vi.mocked(api.aiChatMessages).mockResolvedValue([])
  vi.mocked(api.notificationSummary).mockResolvedValue({
    unread_count: 1,
    high_risk_unread_count: 1,
    latest: [notification],
  })
  vi.mocked(api.notifications).mockResolvedValue({ items: [notification], next_cursor: null })
  vi.mocked(api.markAllNotificationsRead).mockResolvedValue({
    unread_count: 0,
    high_risk_unread_count: 0,
    latest: [],
  })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('chat safety and observability', () => {
  it('renders Markdown without raw HTML, remote images, or unsafe links', () => {
    const { container } = render(
      <SafeMarkdown content={'[安全](https://example.com) [危险](javascript:alert(1)) ![远程](https://example.com/a.png) <script>alert(1)</script>'} />,
    )

    const safe = screen.getByRole('link', { name: '安全' })
    expect(safe).toHaveAttribute('href', 'https://example.com/')
    expect(safe).toHaveAttribute('rel', 'noreferrer noopener')
    expect(screen.getByText('危险').closest('a')).toBeNull()
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('script')).toBeNull()
  })

  it('shows streaming stages, cache status, and a visible degraded-mode warning', async () => {
    vi.mocked(streamAIChat).mockImplementation(async (_threadId, _payload, onEvent) => {
      onEvent({ type: 'stage', stage: 'market', status: 'CACHED', cache_hit: true })
      onEvent({ type: 'stage', stage: 'generation', status: 'DEGRADED' })
      onEvent({ type: 'delta', delta: '**模拟回复**' })
      onEvent({ type: 'done', streaming_mode: 'DEGRADED' })
    })
    vi.mocked(api.aiChatMessages)
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([
        {
          message_id: 'assistant-1',
          thread_id: thread.thread_id,
          role: 'assistant',
          content: '模拟回复',
          status: 'COMPLETED',
          mentioned_symbols: [],
          sources: [],
          cache_hit: false,
          input_tokens: 0,
          output_tokens: 0,
          streaming_mode: 'DEGRADED',
          created_at: '2026-07-20T00:00:00Z',
        },
      ])

    render(<AIChatPage />)
    const composer = await screen.findByPlaceholderText(/输入 @ 后继续输入股票名称/)
    await userEvent.type(composer, '请分析 @600519.SH')
    await userEvent.click(screen.getByRole('button', { name: '发送' }))

    expect(await screen.findByText('流式已降级')).toBeInTheDocument()
    expect(screen.getByText('行情：缓存 · 命中')).toBeInTheDocument()
    expect(screen.getByText('生成：已降级')).toBeInTheDocument()
    expect(await screen.findByText('模拟回复')).toBeInTheDocument()
    expect(streamAIChat).toHaveBeenCalledOnce()
  })

  it('matches personal stocks after @ and inserts the selected stock name', async () => {
    render(<AIChatPage />)
    const composer = await screen.findByPlaceholderText(/输入 @ 后继续输入股票名称/)
    await userEvent.type(composer, '分析 @茅')
    const option = await screen.findByRole('option', { name: /贵州茅台/ })
    await userEvent.click(option)

    expect(composer).toHaveValue('分析 @贵州茅台 ')
    expect(screen.getByText(/候选优先来自持仓和自选/)).toBeInTheDocument()
  })

  it('prefills a quick question without sending it', async () => {
    render(<AIChatPage />)
    const composer = await screen.findByPlaceholderText(/输入 @ 后继续输入股票名称/)
    await userEvent.click(screen.getByRole('button', { name: '生成个股省流版' }))

    expect(composer).toHaveValue('请生成 @股票名称或代码 的省流版，并说明是否适合继续查看模拟方案。')
    expect(streamAIChat).not.toHaveBeenCalled()
  })

  it('shows a high-risk unread badge and marks all notifications read', async () => {
    render(<NotificationBell />)
    const bell = await screen.findByRole('button', { name: '通知中心，1 条未读' })
    expect(bell).toHaveClass('risk')
    expect(screen.getByText('1')).toHaveClass('notification-badge')

    await userEvent.click(bell)
    expect(await screen.findByText('止损预警')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '全部标为已读' }))
    await waitFor(() => {
      expect(api.markAllNotificationsRead).toHaveBeenCalledOnce()
      expect(screen.getByRole('button', { name: '通知中心，0 条未读' })).toBeInTheDocument()
    })
  })
})
