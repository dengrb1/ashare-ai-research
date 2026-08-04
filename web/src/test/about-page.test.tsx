import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { titleForPathname } from '../components/AppShell'
import { AboutPage } from '../pages/AboutPage'

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
}

describe('about page', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      jsonResponse({ status: 'ok', version: '2.0.3', database: 'ok', git_sha: 'a222e6166a0b4b37686aff96fe256c7fc43c7d2f' }),
    ))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    cleanup()
  })

  it('describes the system workflow and investment boundary', async () => {
    render(<MemoryRouter><AboutPage /></MemoryRouter>)

    expect(screen.getByRole('heading', { name: '霁衡智研 · A 股 AI 自动投研系统' })).toBeInTheDocument()
    expect(screen.getByText('冻结数据')).toBeInTheDocument()
    expect(screen.getByText('确定性评分')).toBeInTheDocument()
    expect(screen.getByText(/不构成任何投资建议/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '发起每日研究' })).toHaveAttribute('href', '/research')
  })

  it('shows version, author, license, git hash and release notes from health', async () => {
    render(<MemoryRouter><AboutPage /></MemoryRouter>)

    expect(await screen.findByText('v2.0.3', { selector: 'strong' })).toBeInTheDocument()
    expect(screen.getByText('dengrb')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Apache License 2.0' })).toHaveAttribute('href', 'https://www.apache.org/licenses/LICENSE-2.0')
    expect(screen.getByText('a222e6166a0b')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'v2.0.3' })).toHaveAttribute('href', 'https://github.com/dengrb1/ashare-ai-research/blob/main/docs/releases/v2.0.3.zh-CN.md')
    expect(screen.getByRole('link', { name: 'v2.0.0' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'v1.0.0' })).toBeInTheDocument()
  })

  it('registers the page title including trailing slash paths', () => {
    expect(titleForPathname('/about/')).toEqual(['关于本系统', '系统定位、研究链路与使用边界'])
  })
})
