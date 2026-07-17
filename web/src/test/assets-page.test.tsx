import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { MarketProvider } from '../context/MarketContext'
import { AssetsPage } from '../pages/AssetsPage'

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

it('edits simulated positions and adds watchlist symbols through the persisted asset API', async () => {
  const savedBodies: Array<{ watchlist: string[]; positions: Array<{ quantity: number }> }> = []
  const initial = {
    watchlist: ['600519.SH'],
    positions: [{ symbol: '600519.SH', name: '贵州茅台', quantity: 100, cost: 1400, target_weight: .2 }],
  }
  const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.endsWith('/assets') && init?.method === 'PUT') {
      const body = JSON.parse(String(init.body))
      savedBodies.push(body)
      return jsonResponse(body)
    }
    if (url.endsWith('/assets')) return jsonResponse(initial)
    if (url.includes('/market/quotes')) return jsonResponse([])
    if (url.endsWith('/market/prefetch')) return jsonResponse({ quotes: [], klines: {}, errors: {} })
    throw new Error(`unexpected request ${url}`)
  })
  vi.stubGlobal('fetch', mockFetch)

  render(<MarketProvider><AssetsPage /></MarketProvider>)
  expect(await screen.findByText('贵州茅台')).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: '编辑' }))
  const quantity = screen.getByLabelText('持仓数量')
  await userEvent.clear(quantity)
  await userEvent.type(quantity, '200')
  await userEvent.click(screen.getByRole('button', { name: '保存' }))
  await waitFor(() => expect(savedBodies.some((body) => body.positions[0]?.quantity === 200)).toBe(true))

  await userEvent.type(screen.getByPlaceholderText('输入证券代码，如 600000.SH'), '000001.SZ')
  await userEvent.click(screen.getByRole('button', { name: '添加' }))
  await waitFor(() => expect(savedBodies.some((body) => body.watchlist.includes('000001.SZ'))).toBe(true))

  await userEvent.click(screen.getByRole('button', { name: '新增持仓' }))
  await userEvent.type(screen.getByLabelText('证券代码'), '000001.SZ')
  await userEvent.type(screen.getByLabelText('证券名称'), '平安银行')
  await userEvent.type(screen.getByLabelText('持仓数量'), '100')
  await userEvent.type(screen.getByLabelText('成本价'), '12.5')
  await userEvent.type(screen.getByLabelText('目标权重（%）'), '10')
  await userEvent.click(screen.getByRole('button', { name: '保存' }))
  await waitFor(() => expect(savedBodies.some((body) => body.positions.some((position) => position.quantity === 100))).toBe(true))
  const newPositionRow = screen.getByText('平安银行').closest('tr')
  expect(newPositionRow).not.toBeNull()
  await userEvent.click(within(newPositionRow!).getByRole('button', { name: '删除' }))
  await waitFor(() => expect(savedBodies.at(-1)?.positions).toHaveLength(1))

  await userEvent.click(screen.getByRole('button', { name: '移除 600519.SH' }))
  await waitFor(() => expect(savedBodies.at(-1)?.watchlist).not.toContain('600519.SH'))
})

it('rolls back optimistic asset changes and displays the save error', async () => {
  const initial = {
    watchlist: ['600519.SH'],
    positions: [{ symbol: '600519.SH', name: '贵州茅台', quantity: 100, cost: 1400, target_weight: .2 }],
  }
  const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.endsWith('/assets') && init?.method === 'PUT') return jsonResponse({ detail: '数据库暂不可用' }, 503)
    if (url.endsWith('/assets')) return jsonResponse(initial)
    if (url.includes('/market/quotes')) return jsonResponse([])
    if (url.endsWith('/market/prefetch')) return jsonResponse({ quotes: [], klines: {}, errors: {} })
    throw new Error(`unexpected request ${url}`)
  })
  vi.stubGlobal('fetch', mockFetch)

  render(<MarketProvider><AssetsPage /></MarketProvider>)
  expect(await screen.findByText('贵州茅台')).toBeInTheDocument()
  await userEvent.type(screen.getByPlaceholderText('输入证券代码，如 600000.SH'), '000001.SZ')
  await userEvent.click(screen.getByRole('button', { name: '添加' }))
  expect(await screen.findByText('数据库暂不可用')).toBeInTheDocument()
  expect(screen.queryByText('000001.SZ')).not.toBeInTheDocument()
  expect(screen.getAllByText('600519.SH').length).toBeGreaterThan(0)
})
