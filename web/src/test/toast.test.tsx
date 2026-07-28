import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ErrorNotice } from '../components/Ui'
import { ToastProvider, useToast } from '../context/ToastContext'

function Probe() {
  const { notify } = useToast()
  return <><button onClick={() => notify('已提交', 'success')}>成功</button><button onClick={() => notify('请检查设置', 'warning')}>警告</button><ErrorNotice message="请求失败" /></>
}

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

it('stacks typed toasts and only auto-dismisses success and information messages', async () => {
  vi.useFakeTimers()
  render(<ToastProvider><Probe /></ToastProvider>)

  expect(screen.getByRole('alert')).toHaveTextContent('请求失败')
  await act(async () => { screen.getByRole('button', { name: '成功' }).click() })
  await act(async () => { screen.getByRole('button', { name: '警告' }).click() })
  expect(screen.getByText('已提交').closest('article')).toHaveClass('toast-success')
  expect(screen.getByText('请检查设置').closest('article')).toHaveClass('toast-warning')

  await act(async () => { vi.advanceTimersByTime(4_000) })
  expect(screen.queryByText('已提交')).not.toBeInTheDocument()
  expect(screen.getByText('请检查设置')).toBeInTheDocument()
})
