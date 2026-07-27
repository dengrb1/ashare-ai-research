import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, expect, it, vi } from 'vitest'
import { RefreshProvider } from '../context/RefreshContext'
import { CandidatesPage } from '../pages/CandidatesPage'

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

function wrapper(children: React.ReactNode) {
  return <MemoryRouter><RefreshProvider>{children}</RefreshProvider></MemoryRouter>
}

function installCandidatesFetch(candidates: unknown[]) {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/research/runs?')) return jsonResponse([{ run_id: 'run-1', trading_date: '2026-07-17', status: 'SUCCEEDED' }])
    if (url.includes('/candidates/2026-07-17')) return jsonResponse(candidates)
    if (url.includes('/reports/2026-07-17')) return jsonResponse({ report_id: 'report-1', run_id: 'run-1', trading_date: '2026-07-17', report_type: 'DAILY_RESEARCH' })
    if (url.endsWith('/reports/report-1/symbols')) return jsonResponse([])
    if (url.endsWith('/reports/report-1/trade-plans')) return jsonResponse([])
    throw new Error(`unexpected request ${url}`)
  }))
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

it('maps placeholder industry codes to a friendly missing-data label', async () => {
  installCandidatesFetch([
    { symbol: '600000.SH', name: '浦发银行', rank: 1, total_score: 82, prediction_percentile: .9, industry_code: 'PLACEHOLDER_2', event_risk_multiplier: 1 },
  ])
  render(wrapper(<CandidatesPage />))
  expect(await screen.findByText('行业数据暂缺')).toBeInTheDocument()
  expect(screen.queryByText('PLACEHOLDER_2')).not.toBeInTheDocument()
})

it('prefers the real industry name over the industry code', async () => {
  installCandidatesFetch([
    { symbol: '600000.SH', name: '浦发银行', rank: 1, total_score: 82, prediction_percentile: .9, industry_code: '银行', industry_name: '银行', event_risk_multiplier: 1 },
  ])
  render(wrapper(<CandidatesPage />))
  expect(await screen.findByText('银行')).toBeInTheDocument()
})
