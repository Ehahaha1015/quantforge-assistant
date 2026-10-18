import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ChartDemoNotice, ChartTimeline } from '../src/components/ChartDiagnostics'

afterEach(() => vi.unstubAllGlobals())

it('labels offline evidence explicitly', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => ({ json: async () => ({ demo: true }) })))
  render(<ChartDemoNotice />)
  expect(await screen.findByText('离线工程演示 · 未调用真实模型')).toBeInTheDocument()
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
