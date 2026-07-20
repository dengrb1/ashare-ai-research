import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { RefreshProvider } from '../context/RefreshContext'
import { MarketProvider } from '../context/MarketContext'
import { BacktestPage } from '../pages/BacktestPage'
import { PortfolioPage } from '../pages/PortfolioPage'
import { ReportsPage } from '../pages/ReportsPage'
import { ResearchPage } from '../pages/ResearchPage'

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

function wrapper(children: React.ReactNode) {
  return <MemoryRouter><RefreshProvider>{children}</RefreshProvider></MemoryRouter>
}

function researchWrapper(children: React.ReactNode) {
  return <MemoryRouter><RefreshProvider><MarketProvider>{children}</MarketProvider></RefreshProvider></MemoryRouter>
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('research observation mode and precise reports', () => {
  it('keeps frozen snapshots system-enforced and saves the per-user auto switch', async () => {
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/assets')) return jsonResponse({ watchlist: [], positions: [] })
      if (url.endsWith('/research/settings') && init?.method === 'PUT') return jsonResponse({ auto_enabled: true, automatic_scope: 'MARKET', automatic_total_budget: 1000000, automatic_per_symbol_budget: 80000, schedule_timezone: 'Asia/Shanghai', schedule_time: '15:05', snapshot_mode: 'SYSTEM_ENFORCED' })
      if (url.endsWith('/research/settings')) return jsonResponse({ auto_enabled: false, automatic_scope: 'MARKET', automatic_total_budget: 1000000, automatic_per_symbol_budget: 80000, schedule_timezone: 'Asia/Shanghai', schedule_time: '15:05', snapshot_mode: 'SYSTEM_ENFORCED' })
      if (url.includes('/research/runs?')) return jsonResponse([])
      throw new Error(`unexpected request ${url}`)
    })
    vi.stubGlobal('fetch', mockFetch)
    render(researchWrapper(<ResearchPage />))
    expect(await screen.findByText(/默认关闭 · 15:00 收盘/)).toBeInTheDocument()
    expect(screen.getByText(/系统强制开启（只读）/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: '开启自动日研' }))
    await waitFor(() => expect(screen.getByText(/已开启 · 15:00 收盘/)).toBeInTheDocument())
    const call = mockFetch.mock.calls.find(([url, init]) => String(url).endsWith('/research/settings') && init?.method === 'PUT')
    expect(JSON.parse(String(call?.[1]?.body))).toEqual({ auto_enabled: true })
  })

  it('shows recent FUSED and failed runs with exact report links', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith('/assets')) return jsonResponse({ watchlist: [], positions: [] })
      if (url.includes('/research/runs?')) return jsonResponse([
        { run_id: 'fused-run', trading_date: '2026-07-17', status: 'FUSED', progress: 100, phase: '观察模式报告已发布', started_at: '2026-07-17T10:00:00Z', report_id: 'report-1' },
        { run_id: 'failed-run', trading_date: '2026-07-17', status: 'FAILED', progress: 46, phase: '研究失败', started_at: '2026-07-17T09:00:00Z', error_message: '上游快照校验失败' },
      ])
      throw new Error(`unexpected request ${url}`)
    }))
    render(researchWrapper(<ResearchPage />))
    expect(await screen.findByText('观察模式报告已发布')).toBeInTheDocument()
    expect(screen.getByText('上游快照校验失败')).toBeInTheDocument()
    const link = screen.getByRole('link', { name: /打开本次报告/ })
    expect(link).toHaveAttribute('href', '/reports?date=2026-07-17&run_id=fused-run')
  })

  it('submits custom symbols and personal budget as one frozen research request', async () => {
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/assets')) return jsonResponse({ watchlist: [], positions: [] })
      if (url.includes('/market/quotes')) return jsonResponse([
        { symbol: '600519.SH', name: '贵州茅台', price: 1400, change_pct: 1 },
        { symbol: '000858.SZ', name: '五粮液', price: 120, change_pct: -1 },
      ])
      if (url.includes('/research/runs?')) return jsonResponse([])
      if (url.endsWith('/research/runs') && init?.method === 'POST') return jsonResponse({ run_id: 'custom-run', status: 'PENDING' }, 202)
      throw new Error(`unexpected request ${url} ${init?.method}`)
    })
    vi.stubGlobal('fetch', mockFetch)
    const user = userEvent.setup()
    render(researchWrapper(<ResearchPage />))
    await screen.findByText('暂无研究任务')
    await user.selectOptions(screen.getByLabelText('研究范围'), 'CUSTOM')
    await user.type(screen.getByPlaceholderText(/600519 或/), '600519 000858')
    await user.type(screen.getByLabelText('最高可接受股价（元）'), '500')
    await user.click(screen.getByRole('button', { name: /启动每日研究/ }))
    await waitFor(() => expect(mockFetch.mock.calls.some(([url, init]) => String(url).endsWith('/research/runs') && init?.method === 'POST')).toBe(true))
    const call = mockFetch.mock.calls.find(([url, init]) => String(url).endsWith('/research/runs') && init?.method === 'POST')
    expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({
      scope: 'CUSTOM', symbols: ['600519.SH', '000858.SZ'], total_budget: 1000000,
      per_symbol_budget: 80000, max_stock_price: 500,
    })
  })

  it('selects any subset of watchlist symbols and freezes only checked stocks', async () => {
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/assets')) return jsonResponse({ watchlist: ['600519.SH', '000858.SZ', '300750.SZ'], positions: [] })
      if (url.includes('/market/quotes')) return jsonResponse([
        { symbol: '600519.SH', name: '贵州茅台', price: 1400, change_pct: 1 },
        { symbol: '000858.SZ', name: '五粮液', price: 120, change_pct: -1 },
        { symbol: '300750.SZ', name: '宁德时代', price: 220, change_pct: 0.5 },
      ])
      if (url.endsWith('/research/settings')) return jsonResponse({ auto_enabled: false, portfolio_target_count: 15 })
      if (url.includes('/research/runs?')) return jsonResponse([])
      if (url.endsWith('/research/runs') && init?.method === 'POST') return jsonResponse({ run_id: 'watchlist-run', trading_date: '2026-07-17', status: 'PENDING' }, 202)
      throw new Error(`unexpected request ${url} ${init?.method}`)
    })
    vi.stubGlobal('fetch', mockFetch)
    const user = userEvent.setup()
    render(researchWrapper(<ResearchPage />))
    await screen.findByText('暂无研究任务')
    await user.selectOptions(screen.getByLabelText('研究范围'), 'WATCHLIST')
    expect(await screen.findByText('已选择 3 / 3 只；未勾选股票不会进入本次冻结快照。')).toBeInTheDocument()
    await user.type(screen.getByPlaceholderText('输入股票名称或代码'), '五粮')
    expect(screen.getByText('当前显示 1 只')).toBeInTheDocument()
    await user.click(screen.getByRole('checkbox', { name: /五粮液/ }))
    expect(screen.getByText('已选择 2 / 3 只；未勾选股票不会进入本次冻结快照。')).toBeInTheDocument()
    expect(screen.getByText('预算预览 · 2 只')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /启动每日研究/ }))
    await waitFor(() => expect(mockFetch.mock.calls.some(([url, init]) => String(url).endsWith('/research/runs') && init?.method === 'POST')).toBe(true))
    const call = mockFetch.mock.calls.find(([url, init]) => String(url).endsWith('/research/runs') && init?.method === 'POST')
    expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({
      scope: 'WATCHLIST', symbols: ['600519.SH', '300750.SZ'],
    })
  })

  it('does not submit watchlist research when every stock is unchecked', async () => {
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/assets')) return jsonResponse({ watchlist: ['600519.SH'], positions: [] })
      if (url.includes('/market/quotes')) return jsonResponse([])
      if (url.endsWith('/research/settings')) return jsonResponse({ auto_enabled: false, portfolio_target_count: 15 })
      if (url.includes('/research/runs?')) return jsonResponse([])
      if (url.endsWith('/research/runs') && init?.method === 'POST') throw new Error('empty selection must not submit')
      throw new Error(`unexpected request ${url} ${init?.method}`)
    })
    vi.stubGlobal('fetch', mockFetch)
    const user = userEvent.setup()
    render(researchWrapper(<ResearchPage />))
    await screen.findByText('暂无研究任务')
    await user.selectOptions(screen.getByLabelText('研究范围'), 'WATCHLIST')
    await user.click(screen.getByRole('button', { name: '清空' }))
    await user.click(screen.getByRole('button', { name: /启动每日研究/ }))
    expect(await screen.findByText('请至少选择一只自选股或持仓股票')).toBeInTheDocument()
    expect(mockFetch.mock.calls.some(([url, init]) => String(url).endsWith('/research/runs') && init?.method === 'POST')).toBe(false)
  })

  it('shows a submitted run, lists cross-date activity, and confirms cancellation', async () => {
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true)
    let cancelled = false
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/assets')) return jsonResponse({ watchlist: [], positions: [] })
      if (url.endsWith('/research/settings')) return jsonResponse({ auto_enabled: false, automatic_scope: 'MARKET', automatic_total_budget: 1000000, automatic_per_symbol_budget: 80000, schedule_timezone: 'Asia/Shanghai', schedule_time: '15:05', snapshot_mode: 'SYSTEM_ENFORCED' })
      if (url.endsWith('/research/runs') && init?.method === 'POST') return jsonResponse({ run_id: 'old-run', trading_date: '2026-07-16', status: 'RUNNING' })
      if (url.endsWith('/research/runs/old-run/cancel')) { cancelled = true; return jsonResponse({ run_id: 'old-run', trading_date: '2026-07-16', status: 'CANCEL_REQUESTED' }) }
      if (url.includes('/research/runs?') && !url.includes('trading_date=')) return jsonResponse(cancelled ? [{ run_id: 'old-run', trading_date: '2026-07-16', status: 'CANCEL_REQUESTED', phase: '生成评分', progress: 55, trigger_source: 'AUTO' }] : [{ run_id: 'old-run', trading_date: '2026-07-16', status: 'RUNNING', phase: '生成评分', progress: 55, trigger_source: 'AUTO' }])
      if (url.includes('/research/runs?')) return jsonResponse([])
      throw new Error(`unexpected request ${url}`)
    })
    vi.stubGlobal('fetch', mockFetch)
    render(researchWrapper(<ResearchPage />))
    expect((await screen.findAllByText('old-run')).length).toBeGreaterThan(0)
    expect(screen.getAllByText('自动日研').length).toBeGreaterThan(0)
    await userEvent.click(screen.getByRole('button', { name: '停止任务' }))
    expect(confirm).toHaveBeenCalled()
    expect((await screen.findAllByText('正在停止')).length).toBeGreaterThan(0)
    await userEvent.click(screen.getByRole('button', { name: /启动每日研究/ }))
    expect(await screen.findByText('已复用进行中的研究任务')).toBeInTheDocument()
    expect(screen.getByText(/运行 ID：old-run/)).toBeInTheDocument()
  })

  it('renders stored HTML report inside a sandbox instead of printing tags', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/reports/2026-07-17')) return jsonResponse({ report_id: 'report-1', run_id: 'run-1', trading_date: '2026-07-17', report_type: 'DAILY_RESEARCH' })
      if (url.includes('/research/runs?')) return jsonResponse([{ run_id: 'run-1', status: 'FUSED', reason_code: 'INSUFFICIENT_COMPLETE_SYMBOLS', reason_message: '正式可用股票仅 8 只' }])
      if (url.endsWith('/reports/report-1/content')) return jsonResponse({ content: '<!doctype html><html><body><h1>研究报告正文</h1><p>PLACEHOLDER_3</p></body></html>' })
      throw new Error(`unexpected request ${url}`)
    }))
    render(<MemoryRouter initialEntries={['/reports?date=2026-07-17&run_id=run-1']}><RefreshProvider><ReportsPage /></RefreshProvider></MemoryRouter>)
    const frame = await screen.findByTitle(/A 股每日研究报告正文/)
    expect(frame).toHaveAttribute('sandbox', '')
    expect(frame.getAttribute('srcdoc')).toContain('<h1>研究报告正文</h1>')
    expect(frame.getAttribute('srcdoc')).toContain('行业数据暂缺（占位分组 3）')
    expect(screen.queryByText(/<!doctype html>/)).not.toBeInTheDocument()
  })

  it('submits selected formal report candidates to the asynchronous Trade Plan API', async () => {
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/reports/2026-07-17')) return jsonResponse({ report_id: 'report-1', run_id: 'run-1', trading_date: '2026-07-17', report_type: 'DAILY_RESEARCH' })
      if (url.includes('/research/runs?')) return jsonResponse([{ run_id: 'run-1', trading_date: '2026-07-17', status: 'SUCCEEDED' }])
      if (url.includes('/candidates/2026-07-17')) return jsonResponse([{ symbol: '600000.SH', rank: 1, total_score: 82, base_total_score: 78, dividend_bonus: 4, event_risk_multiplier: 1 }])
      if (url.endsWith('/reports/report-1/content')) return jsonResponse({ content: '<html><body>报告</body></html>' })
      if (url.endsWith('/reports/report-1/trade-plans') && init?.method === 'POST') return jsonResponse({ plan_id: 'plan-1', user_id: 'user-1', report_id: 'report-1', run_id: 'run-1', trading_date: '2026-07-17', decision_at: '2026-07-17T18:00:00+08:00', available_at: '2026-07-17T18:01:00+08:00', status: 'SUCCEEDED', objective: 'RISK_ADJUSTED_RETURN', symbols: ['600000.SH'], snapshot_ids: ['snapshot-1'], optimizer_version: 'v1', config_version: 'v2', input_hash: 'a'.repeat(64), created_at: '2026-07-17T18:01:00+08:00', deterministic_result: { outcome: 'BUY', retained_cash: 1000, symbol_plans: [{ symbol: '600000.SH', action: 'BUY', reason_code: 'QUALIFIED', limit_price_low: 9.8, limit_price_high: 10.2, entry_valid_from: '2026-07-18', entry_valid_until: '2026-07-22', target_weight: 0.08, take_profit_price: 12, stop_loss_price: 9, strategy: { validation_metrics: { net_return: 0.12, sharpe: 1.2, maximum_drawdown: 0.08 } } }] }, ai_explanation: { status: 'SUCCEEDED', items: { '600000.SH': { entry_logic: '等待回踩限价区间', exit_logic: '触发止盈或止损退出', key_evidence: ['基本面质量稳定'], risks: ['市场波动风险'] } } } }, 202)
      if (url.endsWith('/reports/report-1/trade-plans')) return jsonResponse([])
      throw new Error(`unexpected request ${url} ${init?.method}`)
    })
    vi.stubGlobal('fetch', mockFetch)
    render(<MemoryRouter initialEntries={['/reports?date=2026-07-17&run_id=run-1']}><RefreshProvider><ReportsPage /></RefreshProvider></MemoryRouter>)
    expect(await screen.findByText(/历史样本中风险调整后表现最优/)).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: /600000.SH/ })).toHaveAttribute('aria-checked', 'true')
    await userEvent.click(screen.getByRole('button', { name: '生成购买建议' }))
    await waitFor(() => expect(mockFetch.mock.calls.some(([url, init]) => String(url).endsWith('/reports/report-1/trade-plans') && init?.method === 'POST')).toBe(true))
    const call = mockFetch.mock.calls.find(([url, init]) => String(url).endsWith('/reports/report-1/trade-plans') && init?.method === 'POST')
    expect(JSON.parse(String(call?.[1]?.body))).toMatchObject({ symbols: ['600000.SH'], objective: 'RISK_ADJUSTED_RETURN' })
    expect(await screen.findByText('等待回踩限价区间')).toBeInTheDocument()
    expect(screen.getByText('触发止盈或止损退出')).toBeInTheDocument()
    expect(screen.getByText('基本面质量稳定')).toBeInTheDocument()
    expect(screen.getByText('市场波动风险')).toBeInTheDocument()
  })

  it('reuses a successful NO_BUY plan while showing live market and frozen score details', async () => {
    const noBuyPlan = {
      plan_id: 'plan-no-buy', user_id: 'user-1', report_id: 'report-1', run_id: 'run-1',
      trading_date: '2026-07-17', decision_at: '2026-07-17T18:00:00+08:00',
      available_at: '2026-07-17T18:01:00+08:00', status: 'SUCCEEDED',
      objective: 'RISK_ADJUSTED_RETURN', symbols: ['600000.SH'], snapshot_ids: ['snapshot-1'],
      optimizer_version: 'v1', config_version: 'v2', input_hash: 'a'.repeat(64),
      created_at: '2026-07-17T18:01:00+08:00',
      deterministic_result: {
        outcome: 'NO_BUY', retained_cash: 80000,
        symbol_plans: [{
          symbol: '600000.SH', action: 'NO_BUY', reason_code: 'INSUFFICIENT_HISTORY',
          limit_price_low: 9.8, limit_price_high: 10.2,
          entry_valid_from: '2026-07-18', entry_valid_until: '2026-07-22',
          suggested_additional_quantity: 0, target_weight: 0,
          strategy: { validation_metrics: { net_return: -0.01, sharpe: -0.2, maximum_drawdown: 0.13 } },
        }],
      },
      ai_explanation: { status: 'UNAVAILABLE', message: 'AI解释未生成' },
    }
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/reports/2026-07-17')) return jsonResponse({ report_id: 'report-1', run_id: 'run-1', trading_date: '2026-07-17', report_type: 'DAILY_RESEARCH' })
      if (url.includes('/research/runs?')) return jsonResponse([{ run_id: 'run-1', trading_date: '2026-07-17', status: 'SUCCEEDED' }])
      if (url.includes('/candidates/2026-07-17')) return jsonResponse([{ symbol: '600000.SH', rank: 1, total_score: 82, base_total_score: 78, dividend_bonus: 4, prediction_percentile: .91, event_risk_multiplier: .8 }])
      if (url.endsWith('/reports/report-1/content')) return jsonResponse({ content: '<html><body>报告</body></html>' })
      if (url.endsWith('/reports/report-1/trade-plans') && init?.method === 'POST') throw new Error('must reuse successful plan')
      if (url.endsWith('/reports/report-1/trade-plans')) return jsonResponse([noBuyPlan])
      if (url.includes('/market/quotes')) return jsonResponse([{ symbol: '600000.SH', name: '浦发银行', price: 10.1, change_pct: -.5, source: 'AKShare', collected_at: '2026-07-18T07:00:00Z', delayed: true }])
      if (url.includes('/market/klines/600000.SH')) return jsonResponse({ symbol: '600000.SH', period: 'day', adjustment: 'hfq', bars: [{ timestamp: '2026-07-17T07:00:00Z', open: 10, high: 10.3, low: 9.8, close: 10.1, volume: 1000 }], status: { source: 'AKShare', collected_at: '2026-07-18T07:00:00Z', cached_at: '2026-07-18T07:00:01Z', delayed: true } })
      if (url.includes('/scores/2026-07-17/600000.SH/lineage')) return jsonResponse({ formula_version: 'composite-v2' })
      if (url.includes('/scores/2026-07-17/600000.SH')) return jsonResponse({ symbol: '600000.SH', total_score: 82, base_total_score: 78, fundamental_score: 80, technical_score: 84, sentiment_score: 76, quality_confidence_score: 88, dividend_bonus: 4, event_risk_multiplier: .8, formula_version: 'composite-v2' })
      throw new Error(`unexpected request ${url}`)
    })
    vi.stubGlobal('fetch', mockFetch)
    render(<MemoryRouter initialEntries={['/reports?date=2026-07-17&run_id=run-1']}><RefreshProvider><ReportsPage /></RefreshProvider></MemoryRouter>)

    expect(await screen.findByText('浦发银行 · 10.1')).toBeInTheDocument()
    expect(screen.getAllByText('AKShare').length).toBeGreaterThan(0)
    expect(screen.getByText(/延迟数据/)).toBeInTheDocument()
    expect(await screen.findByText('composite-v2')).toBeInTheDocument()
    expect(screen.getByText('NO_BUY · 当前不满足买入条件')).toBeInTheDocument()
    expect(screen.getByText('INSUFFICIENT_HISTORY')).toBeInTheDocument()
    expect(screen.getByText('AI 解释不可用')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '已展示该股方案' })).toBeDisabled()
    expect(mockFetch.mock.calls.some(([url, init]) => String(url).endsWith('/trade-plans') && init?.method === 'POST')).toBe(false)
  })

  it('keeps the FUSED workbench observable when live market requests fail', async () => {
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/reports/2026-07-17')) return jsonResponse({ report_id: 'report-fused', run_id: 'run-fused', trading_date: '2026-07-17', report_type: 'DAILY_RESEARCH' })
      if (url.includes('/research/runs?')) return jsonResponse([{ run_id: 'run-fused', trading_date: '2026-07-17', status: 'FUSED', reason_message: '完整股票不足' }])
      if (url.includes('/candidates/2026-07-17')) return jsonResponse([{ symbol: '600000.SH', rank: 1, total_score: 75, prediction_percentile: .8, event_risk_multiplier: 1 }])
      if (url.endsWith('/reports/report-fused/content')) return jsonResponse({ content: '<html><body>观察报告</body></html>' })
      if (url.endsWith('/reports/report-fused/trade-plans') && init?.method === 'POST') throw new Error('FUSED must not submit')
      if (url.endsWith('/reports/report-fused/trade-plans')) return jsonResponse([])
      if (url.includes('/market/quotes') || url.includes('/market/klines/')) return jsonResponse({ detail: 'market unavailable' }, 503)
      if (url.includes('/scores/2026-07-17/600000.SH/lineage')) return jsonResponse({ formula_version: 'composite-v2' })
      if (url.includes('/scores/2026-07-17/600000.SH')) return jsonResponse({ symbol: '600000.SH', total_score: 75, base_total_score: 72, fundamental_score: 74, technical_score: 78, sentiment_score: 70, quality_confidence_score: 80, dividend_bonus: 3, event_risk_multiplier: 1, formula_version: 'composite-v2' })
      throw new Error(`unexpected request ${url}`)
    })
    vi.stubGlobal('fetch', mockFetch)
    render(<MemoryRouter initialEntries={['/reports?date=2026-07-17&run_id=run-fused']}><RefreshProvider><ReportsPage /></RefreshProvider></MemoryRouter>)

    expect(await screen.findByText('全局风控熔断，禁止生成交易方案')).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: /600000.SH/ })).toBeInTheDocument()
    expect(await screen.findByText('composite-v2')).toBeInTheDocument()
    expect(screen.getByText(/实时行情或 K 线加载失败/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '生成购买建议' })).toBeDisabled()
    expect(mockFetch.mock.calls.some(([url, init]) => String(url).endsWith('/trade-plans') && init?.method === 'POST')).toBe(false)
  })

  it('shows data-limited report stocks as NO_BUY without submitting a plan', async () => {
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/reports/2026-07-17')) return jsonResponse({ report_id: 'report-limited', run_id: 'run-limited', trading_date: '2026-07-17', report_type: 'DAILY_RESEARCH' })
      if (url.includes('/research/runs?')) return jsonResponse([{ run_id: 'run-limited', trading_date: '2026-07-17', status: 'SUCCEEDED', portfolio_generated: false }])
      if (url.endsWith('/reports/report-limited/symbols')) return jsonResponse([{
        symbol: '600001.SH', name: '受限股票', research_status: 'FORMAL_WITH_LIMITATIONS',
        advice_eligible: false, recommendation: 'NO_BUY', exclusion_reasons: ['MISSING_OFFICIAL_DISCLOSURE'],
        data_quality: {}, score: { symbol: '600001.SH', total_score: 60, event_risk_multiplier: 1 },
      }])
      if (url.includes('/candidates/2026-07-17')) return jsonResponse([])
      if (url.endsWith('/reports/report-limited/content')) return jsonResponse({ content: '<html><body>正式报告</body></html>' })
      if (url.endsWith('/reports/report-limited/trade-plans') && init?.method === 'POST') throw new Error('limited stock must not submit')
      if (url.endsWith('/reports/report-limited/trade-plans')) return jsonResponse([])
      if (url.includes('/market/quotes') || url.includes('/market/klines/')) return jsonResponse({ detail: 'market unavailable' }, 503)
      if (url.includes('/scores/2026-07-17/600001.SH')) return jsonResponse({ symbol: '600001.SH', total_score: 60, event_risk_multiplier: 1 })
      throw new Error(`unexpected request ${url}`)
    })
    vi.stubGlobal('fetch', mockFetch)
    render(<MemoryRouter initialEntries={['/reports?date=2026-07-17&run_id=run-limited']}><RefreshProvider><ReportsPage /></RefreshProvider></MemoryRouter>)

    expect(await screen.findByText('数据受限')).toBeInTheDocument()
    expect(screen.getByText(/缺少官方披露/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'NO_BUY' })).toBeDisabled()
    expect(mockFetch.mock.calls.some(([url, init]) => String(url).endsWith('/trade-plans') && init?.method === 'POST')).toBe(false)
  })

  it('renders a FUSED portfolio state without a generic not-found error', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).includes('/research/runs?')) return jsonResponse([{
        run_id: 'fused-run', trading_date: '2026-07-17', status: 'FUSED',
      }])
      if (String(input).includes('/portfolios/')) return jsonResponse({
        run_id: 'fused-run', trading_date: '2026-07-17', status: 'FUSED', observation_only: true,
        message: '风控熔断，当前仅发布观察报告', positions: [], rejection_reasons: [], cash_weight: 1,
      })
      throw new Error(`unexpected request ${String(input)}`)
    }))
    render(wrapper(<PortfolioPage />))
    expect(await screen.findByText('风控熔断，当前仅发布观察报告')).toBeInTheDocument()
    expect(screen.getByDisplayValue('2026-07-17')).toBeInTheDocument()
    expect(vi.mocked(fetch).mock.calls.some(([url]) => String(url).includes('run_id=fused-run'))).toBe(true)
    expect(screen.queryByText(/404/)).not.toBeInTheDocument()
  })
})

describe('backtest task recovery and retry', () => {
  it('restores the latest failed task and retries only after user action', async () => {
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/snapshots?')) return jsonResponse([])
      if (url.endsWith('/backtests') && (!init?.method || init.method === 'GET')) return jsonResponse([
        { backtest_id: 'backtest-1', run_id: 'run-1', status: 'FAILED', name: '失败回测', start_date: '2026-01-01', end_date: '2026-06-30', error_message: 'industry failure', retry_count: 0 },
      ])
      if (url.endsWith('/backtests/backtest-1/retry')) return jsonResponse({
        backtest_id: 'backtest-1', run_id: 'run-1', status: 'PENDING', name: '失败回测', start_date: '2026-01-01', end_date: '2026-06-30', retry_count: 1,
      }, 202)
      if (url.endsWith('/backtests/backtest-1')) return jsonResponse({
        backtest_id: 'backtest-1', run_id: 'run-1', status: 'SUCCEEDED', name: '失败回测', start_date: '2026-01-01', end_date: '2026-06-30', retry_count: 1, metrics: {},
      })
      throw new Error(`unexpected request ${url} ${init?.method}`)
    })
    vi.stubGlobal('fetch', mockFetch)
    render(wrapper(<BacktestPage />))
    expect(await screen.findByText('industry failure')).toBeInTheDocument()
    expect(mockFetch.mock.calls.some(([url]) => String(url).endsWith('/retry'))).toBe(false)
    await userEvent.click(screen.getByRole('button', { name: '重新运行' }))
    await waitFor(() => expect(mockFetch.mock.calls.some(([url]) => String(url).endsWith('/retry'))).toBe(true))
  })
})
