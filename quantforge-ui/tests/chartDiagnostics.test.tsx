import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ChartDemoNotice, ChartTimeline } from '../src/components/ChartDiagnostics'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('labels offline evidence explicitly', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ schema_version: 'qf-health-v1', demo: true }) })))
  render(<ChartDemoNotice />)
  expect(await screen.findByText('离线工程演示 · 未调用真实模型')).toBeInTheDocument()
})

it('keeps mode unknown on failure and recovers with an explicit retry', async () => {
  const mode = vi.fn()
  const fetch = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValue({ ok: true, json: async () => ({ schema_version: 'qf-health-v1', demo: true }) })
  vi.stubGlobal('fetch', fetch)
  render(<ChartDemoNotice onModeChange={mode} />)
  fireEvent.click(await screen.findByRole('button', { name: '重新连接工作区' }))
  expect(await screen.findByText('离线工程演示 · 未调用真实模型')).toBeInTheDocument()
  expect(mode.mock.calls).toEqual([[null], [true]])
})

it('does not trust an error response that claims a live mode', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, json: async () => ({ schema_version: 'qf-health-v1', demo: false }) })))
  render(<ChartDemoNotice />)
  expect(await screen.findByText('暂时无法确认工作区模式')).toBeInTheDocument()
})

it('can retry a failed timeline and refresh newly completed events', async () => {
  let completed = false
  const fetch = vi.fn().mockRejectedValueOnce(new Error('offline')).mockImplementation(async () => ({ ok: true, json: async () => ({ schema_version: 'chart-events-v1', job_id: 'example', items: [{ seq: 1, at: '2026-10-18T08:00:00Z', kind: completed ? 'ANALYSIS_SUCCEEDED' : 'ANALYSIS_STARTED', status: completed ? 'SUCCEEDED' : 'RUNNING', error: null }], note: '脱敏事件' }) }))
  vi.stubGlobal('fetch', fetch)
  const { container } = render(<ChartTimeline id="example" />)
  const details = container.querySelector('details')!
  details.open = true
  fireEvent(details, new Event('toggle'))
  expect(await screen.findByText('任务时间线暂不可用')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '重新读取处理过程' }))
  expect(await screen.findByText('调用已预留并开始')).toBeInTheDocument()
  completed = true
  fireEvent.click(screen.getByRole('button', { name: '刷新处理过程' }))
  expect(await screen.findByText('报告已保存')).toBeInTheDocument()
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3))
})

it('reads a redacted timeline only after expansion', async () => {
  const fetch = vi.fn(async () => ({ ok: true, json: async () => ({ schema_version: 'chart-events-v1', job_id: 'example', items: [{ seq: 1, at: '2026-10-18T08:00:00Z', kind: 'INTERRUPTED_CALL_NOT_REPLAYED', status: 'UNKNOWN', error: null }], note: '未重发' }) }))
  vi.stubGlobal('fetch', fetch)
  const { container } = render(<ChartTimeline id="example" />)
  expect(fetch).not.toHaveBeenCalled()
  const details = container.querySelector('details')!
  details.open = true
  fireEvent(details, new Event('toggle'))
  expect(await screen.findByText('中断已登记，未重发调用')).toBeInTheDocument()
  expect(fetch).toHaveBeenCalledTimes(1)
})
