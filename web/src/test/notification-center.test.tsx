import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Notification, NotificationSummary } from '../types'

const notification: Notification = {
  notification_id: 'notice-1',
  notification_type: 'STOP_LOSS_TRIGGERED',
  severity: 'HIGH',
  title: '止损预警',
  body: '仅模拟退出研究。',
  payload: {},
  created_at: '2026-07-20T00:00:00Z',
  expires_at: '2027-07-20T00:00:00Z',
}

const summary: NotificationSummary = { unread_count: 1, high_risk_unread_count: 1, latest: [notification] }

vi.mock('../api', () => ({
  api: {
    notificationSummary: vi.fn(),
    notifications: vi.fn(),
    markNotificationsRead: vi.fn(),
    markAllNotificationsRead: vi.fn(),
    deleteNotification: vi.fn(),
    clearNotifications: vi.fn(),
  },
}))

import { api } from '../api'
import { NotificationBell } from '../components/NotificationBell'

beforeEach(() => {
  vi.mocked(api.notificationSummary).mockResolvedValue(summary)
  vi.mocked(api.notifications).mockResolvedValue({ items: [notification], next_cursor: null })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('NotificationBell delete and clear', () => {
  it('deletes a single notification and refreshes the badge', async () => {
    vi.mocked(api.deleteNotification).mockResolvedValue(undefined)
    const user = userEvent.setup()
    render(<NotificationBell />)
    await user.click(await screen.findByRole('button', { name: '通知中心，1 条未读' }))
    await waitFor(() => expect(screen.getByText('止损预警')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /删除/ }))
    await waitFor(() => expect(api.deleteNotification).toHaveBeenCalledWith('notice-1'))
    expect(screen.queryByText('止损预警')).not.toBeInTheDocument()
  })

  it('clears all notifications after confirmation', async () => {
    vi.mocked(api.clearNotifications).mockResolvedValue({ unread_count: 0, high_risk_unread_count: 0, latest: [] })
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    const user = userEvent.setup()
    render(<NotificationBell />)
    await user.click(await screen.findByRole('button', { name: '通知中心，1 条未读' }))
    await waitFor(() => expect(screen.getByText('止损预警')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /清空全部通知/ }))
    expect(confirm).toHaveBeenCalled()
    await waitFor(() => expect(api.clearNotifications).toHaveBeenCalled())
    expect(screen.queryByText('止损预警')).not.toBeInTheDocument()
  })
})
