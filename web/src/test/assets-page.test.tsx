import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { MarketProvider, useMarket } from '../context/MarketContext'
import { AssetsPage } from '../pages/AssetsPage'

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

function OrderProbe() {
  const { watchlist } = useMarket()
  return <output data-testid="watch-order">{watchlist.join('>')}</output>
}

function QuoteRefreshProbe() {
  const { refresh } = useMarket()
  return <button onClick={() => void refresh(true)}>刷新行情</button>
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

it('edits holdings and adds watchlist symbols through the persisted asset API', async () => {
  const savedBodies: Array<{ watchlist: string[]; positions: Array<{ quantity: number; target_weight?: number }>; total_assets?: number | null }> = []
  const initial = {
    watchlist: ['600519.SH'],
    total_assets: 200000,
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

  await userEvent.click(screen.getByRole('button', { name: '新增记录' }))
  await userEvent.type(screen.getByLabelText('证券代码'), '000001.SZ')
  await userEvent.type(screen.getByLabelText('证券名称'), '平安银行')
  await userEvent.type(screen.getByLabelText('持仓数量'), '100')
  await userEvent.type(screen.getByLabelText('成本价'), '12.5')
  await userEvent.click(screen.getByRole('button', { name: '保存' }))
  await waitFor(() => expect(savedBodies.some((body) => body.positions.some((position) => position.quantity === 100))).toBe(true))
  expect(savedBodies.find((body) => body.positions.some((position) => position.quantity === 100))?.positions.find((position) => position.quantity === 100)?.target_weight).toBeUndefined()
  const newPositionRow = screen.getByText('平安银行').closest('tr')
  expect(newPositionRow).not.toBeNull()
  await userEvent.click(within(newPositionRow!).getByRole('button', { name: '删除' }))
  await waitFor(() => expect(savedBodies.at(-1)?.positions).toHaveLength(1))

  await userEvent.click(screen.getByRole('button', { name: '移除 600519.SH' }))
  await waitFor(() => expect(savedBodies.at(-1)?.watchlist).not.toContain('600519.SH'))
})

it('calculates current allocation from the latest quote and recalculates when quotes refresh', async () => {
  let price = 1500
  const initial = {
    watchlist: ['600519.SH'],
    total_assets: 200000,
    positions: [{ symbol: '600519.SH', name: '贵州茅台', quantity: 100, cost: 1400 }],
  }
  const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.endsWith('/assets') && (!init?.method || init.method === 'GET')) return jsonResponse(initial)
    if (url.includes('/market/quotes')) return jsonResponse([{ symbol: '600519.SH', name: '贵州茅台', price, change_pct: 1 }])
    if (url.endsWith('/market/prefetch')) return jsonResponse({ quotes: [], klines: {}, errors: {} })
    throw new Error(`unexpected request ${url}`)
  })
  vi.stubGlobal('fetch', mockFetch)

  render(<MarketProvider><QuoteRefreshProbe /><AssetsPage /></MarketProvider>)
  expect(await screen.findByText('75%')).toBeInTheDocument()
  expect(screen.getByText('最新行情')).toBeInTheDocument()

  price = 1600
  await userEvent.click(screen.getByRole('button', { name: '刷新行情' }))
  expect(await screen.findByText('80%')).toBeInTheDocument()
})

it('uses cost-price estimates and prompts for an account total when it is unset', async () => {
  const initial = {
    watchlist: ['600519.SH'],
    positions: [{ symbol: '600519.SH', name: '贵州茅台', quantity: 100, cost: 1400 }],
  }
  const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.endsWith('/assets') && (!init?.method || init.method === 'GET')) return jsonResponse(initial)
    if (url.includes('/market/quotes')) return jsonResponse([])
    if (url.endsWith('/market/prefetch')) return jsonResponse({ quotes: [], klines: {}, errors: {} })
    throw new Error(`unexpected request ${url}`)
  })
  vi.stubGlobal('fetch', mockFetch)

  render(<MarketProvider><AssetsPage /></MarketProvider>)
  expect(await screen.findByText('成本价估算')).toBeInTheDocument()
  expect(screen.getAllByText('请先填写账户总资金').length).toBeGreaterThan(0)
  expect(screen.getByRole('heading', { name: '我的持仓' })).toBeInTheDocument()
})

it('validates and persists the account total explicitly', async () => {
  const savedBodies: Array<{ total_assets?: number | null }> = []
  const initial = { watchlist: [], positions: [] }
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
  const totalAssets = await screen.findByLabelText('账户总资金')
  await userEvent.type(totalAssets, '0')
  await userEvent.click(screen.getByRole('button', { name: '保存账户总资金' }))
  expect(await screen.findByText('账户总资金必须是大于 0 的金额')).toBeInTheDocument()
  expect(savedBodies).toHaveLength(0)

  await userEvent.clear(totalAssets)
  await userEvent.type(totalAssets, '250000')
  await userEvent.click(screen.getByRole('button', { name: '保存账户总资金' }))
  await waitFor(() => expect(savedBodies.at(-1)?.total_assets).toBe(250000))
})

it('rolls the account total back when its save fails', async () => {
  const initial = { watchlist: [], positions: [], total_assets: 200000 }
  const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.endsWith('/assets') && init?.method === 'PUT') return jsonResponse({ detail: '账户总资金保存失败' }, 503)
    if (url.endsWith('/assets')) return jsonResponse(initial)
    if (url.includes('/market/quotes')) return jsonResponse([])
    if (url.endsWith('/market/prefetch')) return jsonResponse({ quotes: [], klines: {}, errors: {} })
    throw new Error(`unexpected request ${url}`)
  })
  vi.stubGlobal('fetch', mockFetch)

  render(<MarketProvider><AssetsPage /></MarketProvider>)
  const totalAssets = await screen.findByLabelText('账户总资金')
  await waitFor(() => expect(totalAssets).toHaveValue(200000))
  await userEvent.clear(totalAssets)
  await userEvent.type(totalAssets, '100000')
  await userEvent.click(screen.getByRole('button', { name: '保存账户总资金' }))
  expect(await screen.findByText('账户总资金保存失败')).toBeInTheDocument()
  await waitFor(() => expect(totalAssets).toHaveValue(200000))
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

it('persists button and drag watchlist ordering and exposes the same order to all market consumers', async () => {
  const savedOrders: string[][] = []
  const initial = { watchlist: ['600519.SH', '000001.SZ', '300750.SZ'], positions: [] }
  const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.endsWith('/assets') && init?.method === 'PUT') {
      const body = JSON.parse(String(init.body))
      savedOrders.push(body.watchlist)
      return jsonResponse(body)
    }
    if (url.endsWith('/assets')) return jsonResponse(initial)
    if (url.includes('/market/quotes')) return jsonResponse([])
    if (url.endsWith('/market/prefetch')) return jsonResponse({ quotes: [], klines: {}, errors: {} })
    throw new Error(`unexpected request ${url}`)
  })
  vi.stubGlobal('fetch', mockFetch)

  render(<MarketProvider><OrderProbe /><AssetsPage /></MarketProvider>)
  expect(await screen.findByTestId('watch-order')).toHaveTextContent('600519.SH>000001.SZ>300750.SZ')
  expect(screen.getByRole('button', { name: '上移 600519.SH' })).toBeDisabled()
  expect(screen.getByRole('button', { name: '下移 300750.SZ' })).toBeDisabled()

  await userEvent.click(screen.getByRole('button', { name: '下移 600519.SH' }))
  await waitFor(() => expect(savedOrders.at(-1)).toEqual(['000001.SZ', '600519.SH', '300750.SZ']))
  expect(screen.getByTestId('watch-order')).toHaveTextContent('000001.SZ>600519.SH>300750.SZ')

  const dataTransfer = { effectAllowed: '', setData: vi.fn(), getData: vi.fn() }
  const dragged = screen.getByRole('button', { name: '拖拽排序 300750.SZ' })
  const target = screen.getByRole('button', { name: '拖拽排序 000001.SZ' }).parentElement!
  fireEvent.dragStart(dragged, { dataTransfer })
  fireEvent.dragOver(target, { dataTransfer })
  fireEvent.drop(target, { dataTransfer })
  await waitFor(() => expect(savedOrders.at(-1)).toEqual(['300750.SZ', '000001.SZ', '600519.SH']))
  expect(screen.getByTestId('watch-order')).toHaveTextContent('300750.SZ>000001.SZ>600519.SH')
})

it('rolls back a failed reorder and disables concurrent watchlist mutations while saving', async () => {
  let rejectSave: ((response: Response) => void) | undefined
  const initial = { watchlist: ['600519.SH', '000001.SZ'], positions: [] }
  const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if (url.endsWith('/assets') && init?.method === 'PUT') return new Promise<Response>((resolve) => { rejectSave = resolve })
    if (url.endsWith('/assets')) return jsonResponse(initial)
    if (url.includes('/market/quotes')) return jsonResponse([])
    if (url.endsWith('/market/prefetch')) return jsonResponse({ quotes: [], klines: {}, errors: {} })
    throw new Error(`unexpected request ${url}`)
  })
  vi.stubGlobal('fetch', mockFetch)

  render(<MarketProvider><OrderProbe /><AssetsPage /></MarketProvider>)
  expect(await screen.findByTestId('watch-order')).toHaveTextContent('600519.SH>000001.SZ')
  await userEvent.click(screen.getByRole('button', { name: '下移 600519.SH' }))
  expect(screen.getByTestId('watch-order')).toHaveTextContent('000001.SZ>600519.SH')
  expect(screen.getByRole('button', { name: '添加' })).toBeDisabled()
  expect(screen.getByRole('button', { name: '移除 600519.SH' })).toBeDisabled()
  rejectSave?.(jsonResponse({ detail: '排序保存失败' }, 503))
  expect(await screen.findByText('排序保存失败')).toBeInTheDocument()
  await waitFor(() => expect(screen.getByTestId('watch-order')).toHaveTextContent('600519.SH>000001.SZ'))
})
