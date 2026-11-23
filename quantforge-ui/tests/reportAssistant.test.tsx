import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ReportAssistant } from '../src/components/ReportAssistant'
import { ChartSynthesis } from '../src/components/ChartSynthesis'
import { ChartTradePlan } from '../src/components/ChartTradePlan'
import { parseAnswer, parseBrief } from '../src/data/chartAssistant'
import { parseAnalysis } from '../src/data/chartReports'
import { synthesisFixture } from './fixtures/synthesisFixture'

const analysis = () => parseAnalysis(structuredClone(synthesisFixture))
const sha = 'f'.repeat(64)
const evidence = [{ id: 'e1', label: '规则过滤原因', kind: 'COMPUTATION', path: '/algorithm_analysis/trade_plan', value: { action: 'WAIT' }, note: '固定规则归档证据。' }]
const claim = { text: '趋势与入场许可分别判断，量能确认尚不足。', evidence_ids: ['e1'] }
const brief = () => ({ schema_version: 'chart-assistant-brief-v1', job_id: analysis().id, snapshot_sha256: sha, status: 'READY', status_label: '可解释归档证据 · 预测未验证', as_of: '2026-09-08', offline: true, headline: '固定规则倾向', focus_question: '为什么等待？', supporting: [claim], opposing: [], next_checks: [claim], missing: [claim], evidence, scope_note: '归档时点，不是当前行情。', model_calls: 0, forecast_validated: false })
const answer = (question: string) => ({ schema_version: 'chart-assistant-answer-v1', job_id: analysis().id, snapshot_sha256: sha, question, intent: 'WHY', status: 'ANSWERED', claims: [claim], evidence, note: '没有重新识图或取数。', model_calls: 0, forecast_validated: false })
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('loads a source-bound overview and reveals cited archived fields on click', async () => {
  const fetch = vi.fn(async () => ({ ok: true, json: async () => brief() }))
  vi.stubGlobal('fetch', fetch)
  const { container } = render(<ReportAssistant analysis={analysis()} items={[]} />)
  await screen.findByText('可解释归档证据 · 预测未验证')
  expect(fetch).toHaveBeenCalledTimes(1)
  fireEvent.click(screen.getAllByRole('link', { name: '依据 e1' })[0])
  const target = container.querySelector(`#brief-${analysis().id}-e1`) as HTMLDetailsElement
  expect(target.open).toBe(true)
  expect(target.closest('.assistant-evidence-index')).toHaveAttribute('open')
  expect(screen.getByText('/algorithm_analysis/trade_plan')).toBeInTheDocument()
})

it('asks only after user action and checks snapshot identity before showing an answer', async () => {
  const writes: RequestInit[] = []
  vi.stubGlobal('fetch', vi.fn(async (_url: string, options?: RequestInit) => {
    if (options?.method === 'POST') { writes.push(options); return { ok: true, json: async () => answer(JSON.parse(String(options.body)).question) } }
    return { ok: true, json: async () => brief() }
  }))
  render(<ReportAssistant analysis={analysis()} items={[]} />)
  await screen.findByText('可解释归档证据 · 预测未验证')
  expect(writes).toHaveLength(0)
  fireEvent.click(screen.getByRole('button', { name: '为什么偏强却要等待？' }))
  expect(await screen.findByText('没有重新识图或取数。')).toBeInTheDocument()
  expect(writes).toHaveLength(1)
  expect(writes[0].headers).toEqual({ 'Content-Type': 'application/json', 'X-QF-Mode': 'RESEARCH' })
})

it('does not carry a pending answer into a different selected report', async () => {
  let resolve!: (value: unknown) => void
  vi.stubGlobal('fetch', vi.fn(async (url: string, options?: RequestInit) => {
    if (options?.method === 'POST') return new Promise(r => { resolve = r })
    const id = url.split('/').at(-2)
    return { ok: true, json: async () => ({ ...brief(), job_id: id }) }
  }))
  const { rerender } = render(<ReportAssistant analysis={analysis()} items={[]} />)
  await screen.findByText('可解释归档证据 · 预测未验证')
  fireEvent.click(screen.getByRole('button', { name: '为什么偏强却要等待？' }))
  const next = { ...analysis(), id: 'chart-' + 'a'.repeat(32) }
  rerender(<ReportAssistant analysis={next} items={[]} />)
  await waitFor(() => expect(screen.getByRole('button', { name: '为什么偏强却要等待？' })).not.toBeDisabled())
  resolve({ ok: true, json: async () => answer('为什么偏强却要等待？') })
  await waitFor(() => expect(screen.queryByText('没有重新识图或取数。')).not.toBeInTheDocument())
})

it('rejects dangling citations, wrong report identity and fabricated validation', () => {
  expect(parseBrief(brief(), analysis().id).model_calls).toBe(0)
  expect(() => parseBrief({ ...brief(), supporting: [{ ...claim, evidence_ids: ['missing'] }] }, analysis().id)).toThrow()
  expect(() => parseBrief({ ...brief(), forecast_validated: true }, analysis().id)).toThrow()
  expect(() => parseAnswer(answer('why'), 'different-report', 'why')).toThrow()
  expect(() => parseAnswer({ ...answer('why'), snapshot_sha256: 'bad' }, analysis().id, 'why')).toThrow()
})

it('suppresses old ready conclusions and exact levels when the current data guard blocks them', () => {
  const value = analysis()
  value.request.adjustment = 'RAW'; value.report!.recognized_adjustment = 'RAW'
  value.analysis_guard = { status: 'DATA_UNAVAILABLE', note: '行情日历尚未核验', snapshot_sha256: sha }
  render(<><ChartSynthesis analysis={value} /><ChartTradePlan analysis={value} /></>)
  expect(screen.queryByText('200.0000')).not.toBeInTheDocument()
  expect(screen.getByText('先核对截图，当前无法形成有效综合结论。')).toBeInTheDocument()
})
