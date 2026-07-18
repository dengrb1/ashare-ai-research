import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { App } from '../App'
import { titleForPathname } from '../components/AppShell'

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

describe('login flow', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('restores unauthenticated state and submits built-in account login', async () => {
    const mockFetch = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/auth/me')) return jsonResponse({ detail: 'authentication required' }, 401)
      if (url.endsWith('/auth/login')) return jsonResponse({ user_id: 'u1', username: 'analyst', role: 'USER', enabled: true, created_at: '2026-07-15T00:00:00Z' })
      if (url.endsWith('/assets')) return jsonResponse({ watchlist: ['600519.SH'], positions: [] })
      if (url.endsWith('/market/prefetch')) return jsonResponse({ quotes: [], klines: {}, errors: {} })
      return jsonResponse([])
    })
    vi.stubGlobal('fetch', mockFetch)

    render(<MemoryRouter initialEntries={['/login']}><App /></MemoryRouter>)
    const username = await screen.findByPlaceholderText('请输入用户名')
    await userEvent.type(username, 'analyst')
    await userEvent.type(screen.getByPlaceholderText('请输入密码'), 'secure-password')
    await userEvent.click(screen.getByRole('button', { name: /进入终端/ }))

    expect(await screen.findByRole('heading', { name: '全局仪表盘' })).toBeInTheDocument()
    const loginCall = mockFetch.mock.calls.find(([url]) => String(url).endsWith('/auth/login'))
    expect(loginCall?.[1]).toMatchObject({ method: 'POST', credentials: 'include' })
    await waitFor(() => expect(mockFetch.mock.calls.some(([url, init]) => String(url).endsWith('/market/prefetch') && init?.method === 'POST')).toBe(true))
  })

  it('keeps the assets page heading when nginx redirects to a trailing slash', () => {
    expect(titleForPathname('/assets/')).toEqual(['自选与持仓', '关注列表与个人持仓记录'])
  })
})
