import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'
import { titleForPathname } from '../components/AppShell'
import { AboutPage } from '../pages/AboutPage'

describe('about page', () => {
  it('describes the system workflow and investment boundary', () => {
    render(<MemoryRouter><AboutPage /></MemoryRouter>)

    expect(screen.getByRole('heading', { name: '霁衡智研 · A 股 AI 自动投研系统' })).toBeInTheDocument()
    expect(screen.getByText('冻结数据')).toBeInTheDocument()
    expect(screen.getByText('确定性评分')).toBeInTheDocument()
    expect(screen.getByText(/不构成任何投资建议/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '发起每日研究' })).toHaveAttribute('href', '/research')
  })

  it('registers the page title including trailing slash paths', () => {
    expect(titleForPathname('/about/')).toEqual(['关于本系统', '系统定位、研究链路与使用边界'])
  })
})
