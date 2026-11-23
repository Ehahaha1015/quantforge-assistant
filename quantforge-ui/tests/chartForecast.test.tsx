import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ChartForecast, ForecastPlot } from '../src/components/ChartForecast'
import { parseAnalysis } from '../src/data/chartReports'
import { parseVisualization, type ChartVisualization } from '../src/data/chartVisualization'
import { synthesisFixture } from './fixtures/synthesisFixture'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })
function analysis() { const a = parseAnalysis(structuredClone(synthesisFixture)); a.request.adjustment = 'RAW'; a.report!.recognized_adjustment = 'RAW'; return a }
function chart(): ChartVisualization {
  const a = analysis(), reference = 200, atr = 3
  return {
    schema_version: 'chart-visualization-v1', job_id: a.id, snapshot_sha256: 'f'.repeat(64), status: 'AVAILABLE', reason: null,
    symbol: '600519', timeframe: '1d', horizon_bars: 5, direction: 'RANGE', as_of: '2026-09-08', offline: true,
    source_sha256: 'a'.repeat(64), bars_sha256: 'b'.repeat(64),
    candles: Array.from({ length: 80 }, (_, i) => ({ at: new Date(Date.UTC(2026, 5, 21 + i, 7)).toISOString(), label: new Date(Date.UTC(2026, 5, 21 + i, 7)).toISOString().slice(0, 10), open: String(120 + i), high: String(123 + i), low: String(119 + i), close: String(121 + i), sma20: String(115 + i), sma60: String(105 + i) })),
    boundaries: Array.from({ length: 6 }, (_, i) => ({ step: i, upper: String(reference + .5 * atr * Math.sqrt(i)), reference: '200', lower: String(reference - .5 * atr * Math.sqrt(i)) })),
    levels: { reference: '200', stop_reference: '194', take_profit_reference: '209', rule: '已有ATR条件参考，不是目标价格。' },
    atr14: '3', reference_close: '200', formula: '归档收盘价 ± 0.5 × ATR14 × √后续K线根数', interpretation: '判定边界不是预测轨迹。',
    model_calls: 0, market_fetches: 0, forecast_validated: false,
  }
}

it('validates every candle, ordered timestamp and exact existing threshold formula', () => {
  const v = chart()
  expect(parseVisualization(v, v.job_id).candles).toHaveLength(80)
  const invalids = [
    { ...v, forecast_validated: true }, { ...v, market_fetches: 1 }, { ...v, job_id: 'other' },
    { ...v, horizon_bars: 20 }, { ...v, reference_close: '201' },
    { ...v, boundaries: v.boundaries.map(b => b.step === 5 ? { ...b, upper: '999' } : b) },
    { ...v, candles: v.candles.map((c, i) => i === 2 ? { ...c, high: '1' } : c) },
    { ...v, candles: v.candles.map((c, i) => i === 2 ? { ...c, at: v.candles[0].at } : c) },
    { ...v, status: 'BLOCKED', reason: 'missing' },
  ]
  for (const bad of invalids) expect(() => parseVisualization(bad, v.job_id)).toThrow()
})

it('shows archived candles and interactive conditional regions without inventing probabilities', () => {
  render(<ForecastPlot view={chart()} analysis={analysis()} />)
  expect(screen.getByRole('group', { name: /历史K线与未来5根条件分区/ })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '查看偏强情景' }))
  expect(screen.getByText('触发条件：至少两组偏强且无分歧，较长周期与风险确认。')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '查看后续第1根的判定边界' }))
  expect(screen.getByText('偏强：高于 201.5')).toBeInTheDocument()
  expect(screen.getByText('偏弱：低于 198.5')).toBeInTheDocument()
  const candle = screen.getAllByRole('button', { name: /开盘.*最高.*最低.*收盘/ })[0]
  fireEvent.focus(candle)
  expect(screen.getByText(/开 160/)).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('历史窗口'), { target: { value: '20' } })
  expect(screen.getAllByRole('button', { name: /开盘.*最高.*最低.*收盘/ })).toHaveLength(20)
  expect(screen.queryByText(/胜率 \d/)).not.toBeInTheDocument()
})

it('does not request or display a price visualization when metadata alignment is blocked', () => {
  const a = analysis(); a.report!.recognized_adjustment = 'QFQ'
  const fetch = vi.fn(); vi.stubGlobal('fetch', fetch)
  render(<ChartForecast analysis={a} />)
  expect(screen.getByText(/先核对截图与行情，暂不绘制价格情景/)).toBeInTheDocument()
  expect(fetch).not.toHaveBeenCalled()
  expect(screen.queryByRole('group', { name: /历史K线/ })).not.toBeInTheDocument()
})

it('loads only the saved source and rejects a response belonging to another report version', async () => {
  const a = analysis(); a.analysis_guard = { status: 'READY', note: '核对通过', snapshot_sha256: 'e'.repeat(64) }
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => chart() })))
  render(<ChartForecast analysis={a} />)
  expect(await screen.findByRole('alert')).toHaveTextContent('来源或版本不一致')
  expect(screen.queryByRole('group', { name: /历史K线/ })).not.toBeInTheDocument()
})

it('progressively renders image scenarios when dates or timeframe are missing', async () => {
  const a = analysis()
  a.report!.recognized_period = null
  a.report!.recognized_timeframe = null
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ ...chart(), status: 'BLOCKED', reason: '日期未明确', candles: [], boundaries: [], levels: null, atr14: null, reference_close: null }) })))
  render(<ChartForecast analysis={a} progressive />)
  expect(await screen.findByRole('img', { name: /基于截图观察的三种条件情景示意/ })).toBeInTheDocument()
  expect(screen.queryByRole('group', { name: /历史K线/ })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '查看偏弱情景' }))
  expect(screen.getByText('触发：仅在条件成立时。')).toBeInTheDocument()
})

it('shows adjusted-image background with warnings and without exact trade references', async () => {
  const a = analysis()
  a.request.adjustment = 'QFQ'; a.report!.recognized_adjustment = 'QFQ'
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ ...chart(), view_mode: 'BACKGROUND', warnings: ['截图为前复权，背景为不复权'], direction: 'UNCLEAR', levels: null }) })))
  render(<ChartForecast analysis={a} progressive />)
  expect(await screen.findByRole('group', { name: /历史K线与未来5根条件分区/ })).toBeInTheDocument()
  expect(screen.getByText('截图为前复权，背景为不复权')).toBeInTheDocument()
  expect(screen.queryByLabelText('条件参考线')).not.toBeInTheDocument()
  expect(screen.queryByText(/止损参考 194/)).not.toBeInTheDocument()
  expect(() => parseVisualization({ ...chart(), view_mode: 'BACKGROUND' }, a.id)).toThrow('不能携带精确买卖参考')
})
