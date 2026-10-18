import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { ChartReports } from '../src/pages/ChartReports'
import { parseAlgorithms, parseAnalysis, parseProvider, refusalText } from '../src/data/chartReports'
import { ChartAlgorithms } from '../src/components/ChartAlgorithms'
import ChartApp from '../src/ChartApp'

const provider = { schema_version: 'chart-provider-status-v1', provider: 'DeepSeek', model: 'deepseek-flash', configured: false, ready: false, max_cost_cny: '0.00', reserved_cny: '0', calls_used: 0, calls_limit: 100, qq_connected: false, strategy_version: 'chart-scenarios-v1' }
const request = { symbol: 'TEST', display_name: '', period_start: '2026-09-01', period_end: '2026-09-30', horizon_sessions: 5, question: '软件夹具', channel: 'WEB' }
const analysis = { schema_version: 'chart-analysis-v1', id: 'chart-' + 'a'.repeat(32), status: 'WAITING_FOR_MODEL', request, image: { sha256: 'b'.repeat(64), bytes: 500, width: 400, height: 240, mime: 'image/png' }, created_at: '2026-10-16T01:00:00Z', updated_at: '2026-10-16T01:00:00Z', report: null, strategy_version: 'chart-scenarios-v1', error: null, model_call: null, live_authority: 'NONE', forecast_validated: false }
let received: { url: string; options?: RequestInit }[]
beforeEach(() => {
  received = []
  const NativeURL = URL
  vi.stubGlobal('URL', class extends NativeURL { static createObjectURL() { return 'blob:fixture-preview' } static revokeObjectURL() {} })
  vi.stubGlobal('fetch', vi.fn(async (url: string, options?: RequestInit) => {
    received.push({ url, options })
    const value = url.endsWith('/api/chart-settings') ? provider : options?.method === 'POST' ? analysis : url.endsWith('/api/chart-analyses') ? { schema_version: 'chart-list-v1', items: [] } : analysis
    return { ok: true, json: async () => value }
  }))
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

it('offers real upload with unconfigured status and never presents a fabricated report', async () => {
  render(<ChartReports />)
  expect(await screen.findByText('报告服务在线')).toBeInTheDocument()
  expect(screen.getByText('待配置')).toBeInTheDocument()
  expect(screen.getByText('QQ未连接或等待扫码 · 交易权限关闭')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '保存截图，稍后生成' })).toBeDisabled()
  expect(screen.queryByText('分析摘要')).not.toBeInTheDocument()
  expect(received.every(row => !row.options?.method)).toBe(true)
  expect(screen.getByLabelText('图中K线周期')).toHaveValue('1d')
})

it('changes hourly/weekly horizons and clears holding counts across frequencies', async () => {
  render(<ChartReports />)
  await screen.findByText('报告服务在线')
  fireEvent.change(screen.getByLabelText('图中K线周期'),{target:{value:'60m'}})
  expect(screen.getByLabelText('图中截止时间（北京时间）')).toBeInTheDocument()
  expect(screen.getByLabelText('后续走势观察范围')).toHaveValue('4')
  fireEvent.change(screen.getByLabelText('购入成本'),{target:{value:'100'}})
  fireEvent.change(screen.getByLabelText('已持有的完整K线数'),{target:{value:'3'}})
  fireEvent.change(screen.getByLabelText('图中K线周期'),{target:{value:'1w'}})
  expect(screen.getByLabelText('后续走势观察范围')).toHaveValue('1')
  expect(screen.getByLabelText('已持有的完整K线数')).toHaveValue(0)
  expect(screen.queryByLabelText('图中截止时间（北京时间）')).not.toBeInTheDocument()
  expect(received.every(row => !row.options?.method)).toBe(true)
})

it('saves the selected image under one idempotent request and shows waiting state', async () => {
  render(<ChartReports />)
  await screen.findByText('报告服务在线')
  fireEvent.change(screen.getByLabelText('上传股票走势图'), { target: { files: [new File(['software fixture'], 'chart.png', { type: 'image/png' })] } })
  fireEvent.change(screen.getByLabelText('股票代码或名称'), { target: { value: 'TEST' } })
  fireEvent.change(screen.getByLabelText('图中复权方式'), { target: { value: 'RAW' } })
  fireEvent.change(screen.getByLabelText('最长持仓观察（完整日线根数）'), { target: { value: '10' } })
  fireEvent.click(screen.getByLabelText('我已确认股票、K线周期和截止时间与截图一致。'))
  fireEvent.click(screen.getByRole('button', { name: '保存截图，稍后生成' }))
  expect(await screen.findByText('截图已保存，等待识图服务')).toBeInTheDocument()
  const writes = received.filter(row => row.options?.method === 'POST')
  expect(writes).toHaveLength(1)
  const headers = writes[0].options?.headers as Record<string, string>
  expect(headers['X-QF-Mode']).toBe('RESEARCH')
  expect(headers['Idempotency-Key']).toBeTruthy()
  expect(JSON.parse(String(writes[0].options?.body))).toMatchObject({ symbol: 'TEST', channel: 'WEB', adjustment:'RAW', horizon_bars:5, max_holding_bars:10, review_interval_bars:1 })
  expect(screen.getByRole('button', { name: '生成这份报告' })).toBeDisabled()
})

it('rejects oversize inputs before submitting', async () => {
  render(<ChartReports />)
  await screen.findByText('报告服务在线')
  const file = new File(['fixture'], 'large.png', { type: 'image/png' })
  Object.defineProperty(file, 'size', { value: 6 * 1024 * 1024 })
  fireEvent.change(screen.getByLabelText('上传股票走势图'), { target: { files: [file] } })
  expect(await screen.findByRole('alert')).toHaveTextContent('不超过5MB')
  await waitFor(() => expect(received.filter(row => row.options?.method === 'POST')).toHaveLength(0))
})

it.each(['invalid-json', 'server-error', 'store-error'])('keeps one submission key after an ambiguous response: %s', async failure => {
  const originalFetch = globalThis.fetch
  let attempts = 0
  const keys: string[] = [], bodies: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, options?: RequestInit) => {
    if (options?.method === 'POST') {
      keys.push((options.headers as Record<string, string>)['Idempotency-Key'])
      bodies.push(String(options.body))
      if (++attempts === 1) {
        if (failure === 'invalid-json') return { ok: true, status: 200, json: async () => { throw new SyntaxError('truncated') } }
        return { ok: false, status: failure === 'server-error' ? 502 : 409, json: async () => ({ error: failure === 'store-error' ? 'CHART_INPUT_OR_STORE_UNAVAILABLE' : 'UPSTREAM_FAILED' }) }
      }
    }
    return originalFetch(url, options)
  }))
  render(<ChartReports />)
  await screen.findByText('报告服务在线')
  fireEvent.change(screen.getByLabelText('上传股票走势图'), { target: { files: [new File(['software fixture'], 'chart.png', { type: 'image/png' })] } })
  fireEvent.change(screen.getByLabelText('股票代码或名称'), { target: { value: 'TEST' } })
  fireEvent.click(screen.getByLabelText('我已确认股票、K线周期和截止时间与截图一致。'))
  fireEvent.click(screen.getByRole('button', { name: '保存截图，稍后生成' }))
  const retry = await screen.findByRole('button', { name: '核查同一次提交' })
  await waitFor(() => expect(retry).not.toBeDisabled())
  expect(screen.getByRole('button', { name: '保存截图，稍后生成' })).toBeDisabled()
  expect(screen.getByLabelText('股票代码或名称')).toBeDisabled()
  fireEvent.click(retry)
  expect(await screen.findByText('截图已保存，等待识图服务')).toBeInTheDocument()
  expect(keys).toHaveLength(2)
  expect(keys[0]).toBeTruthy()
  expect(keys[1]).toBe(keys[0])
  expect(bodies[1]).toBe(bodies[0])
})

it('rejects secret-bearing provider status and misleading forecast qualifications', () => {
  expect(parseProvider(provider).configured).toBe(false)
  expect(() => parseProvider({ ...provider, api_key: 'fixture-only' })).toThrow()
  expect(() => parseAnalysis({ ...analysis, forecast_validated: true })).toThrow()
  expect(() => parseAnalysis({ ...analysis, status: 'SUCCEEDED' })).toThrow('完成任务缺少报告')
})

it('renders an ambiguous call as needing review without a retry button', async () => {
  const unknown = { ...analysis, status: 'UNKNOWN', error: 'CHART_PROVIDER_EFFECT_UNKNOWN' }
  vi.stubGlobal('fetch', vi.fn(async (url: string) => ({ ok: true, json: async () => url.endsWith('/api/chart-settings') ? { ...provider, configured: true, ready: true } : url.endsWith('/api/chart-analyses') ? { schema_version: 'chart-list-v1', items: [unknown] } : unknown })))
  render(<ChartReports />)
  fireEvent.click(await screen.findByRole('button', { name: /TEST/ }))
  expect(await screen.findByText('调用结果未知，已停止自动重发')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '生成这份报告' })).not.toBeInTheDocument()
})

it('explains why a report stopped instead of showing a bare error code', async () => {
  const failed = { ...analysis, status: 'FAILED', error: 'CHART_ANALYSIS_FAILED_NO_AUTOMATIC_RETRY' }
  vi.stubGlobal('fetch', vi.fn(async (url: string) => ({ ok: true, json: async () => url.endsWith('/api/chart-settings') ? { ...provider, configured: true, ready: true } : url.endsWith('/api/chart-analyses') ? { schema_version: 'chart-list-v1', items: [failed] } : failed })))
  render(<ChartReports />)
  fireEvent.click(await screen.findByRole('button', { name: /TEST/ }))
  expect(await screen.findByText('本次生成未完成，系统不会自动重试。')).toBeInTheDocument()
  expect(screen.queryByText('CHART_ANALYSIS_FAILED_NO_AUTOMATIC_RETRY')).not.toBeInTheDocument()
})

it('has a local-language message for every code the chart API can refuse with', () => {
  const codes = ['BOUNDED_JSON_REQUIRED', 'OBJECT_REQUIRED', 'EXPLICIT_LOCAL_MODE_REQUIRED', 'LOCAL_ORIGIN_REQUIRED', 'SCREENSHOT_PRODUCT_ONLY', 'CHART_API_KEY_REQUIRED', 'CHART_API_KEY_INVALID', 'CHART_BUDGET_EXHAUSTED', 'CHART_BUDGET_INVALID', 'CHART_BUDGET_CANNOT_RESET', 'CHART_CONFIGURATION_BUSY', 'CHART_CONFIGURATION_INVALID', 'CHART_IMAGE_SIZE_INVALID', 'CHART_IMAGE_FORMAT_INVALID', 'CHART_IMAGE_DIMENSIONS_INVALID', 'CHART_IMAGE_ENCODING_INVALID', 'CHART_IMAGE_INVALID', 'CHART_IMAGE_INTEGRITY_FAILED', 'CHART_INPUT_OR_STORE_UNAVAILABLE', 'CHART_IDEMPOTENCY_KEY_REQUIRED', 'CHART_IDEMPOTENCY_CONFLICT', 'CHART_CHANNEL_NOT_CONFIGURED', 'CHART_ID_INVALID', 'CHART_NOT_FOUND', 'CHART_OR_REPORT_UNAVAILABLE', 'CHART_START_REQUIRES_WAITING', 'CHART_PRIOR_CALL_REQUIRES_REVIEW', 'CHART_WORKSPACE_FULL', 'CHART_HISTORY_INTEGRITY_FAILED', 'CHART_PROJECTION_INTEGRITY_FAILED', 'CHART_MARKET_DATA_INTEGRITY_FAILED', 'CHART_SECRET_STORE_UNAVAILABLE', 'CHART_SECRET_STORE_REDIRECTED', 'CHART_PROVIDER_REJECTED', 'CHART_PROVIDER_INVALID_REPORT', 'CHART_PROVIDER_RESPONSE_TOO_LARGE', 'CHART_PROVIDER_EFFECT_UNKNOWN', 'CHART_ANALYSIS_FAILED_NO_AUTOMATIC_RETRY']
  for (const code of codes) expect(refusalText(code), code).not.toBe('')
  expect(refusalText('NOT_A_REAL_CODE')).toBe('')
})

it('removes old research and money navigation from the screenshot product', async () => {  render(<ChartApp />)
  expect(await screen.findByText('报告服务在线')).toBeInTheDocument()
  expect(screen.getByRole('heading', { name: /^股票截图报告$/ })).toBeInTheDocument()
  expect(screen.queryByRole('link', { name: '资金与自动运行' })).not.toBeInTheDocument()
  expect(screen.queryByRole('link', { name: '研究任务' })).not.toBeInTheDocument()
})

it('shows failed data retrieval honestly and rejects invented algorithm authority', () => {
  const data = { schema_version: 'core-chart-strategies-v1', status: 'UNAVAILABLE', reason: '行情请求失败', source: null, strategies: [], baseline: [], limitations: [], forecast_validated: false, screenshot_identity_verified: false }
  render(<ChartAlgorithms analysis={parseAlgorithms(data)} />)
  expect(screen.getByText(/行情请求失败/)).toBeInTheDocument()
  expect(screen.queryByText('规则下研究持仓')).not.toBeInTheDocument()
  expect(() => parseAlgorithms({ ...data, forecast_validated: true })).toThrow()
  expect(() => parseAlgorithms({ ...data, status: 'AVAILABLE' })).toThrow()
})

it('keeps Core flat state separate from a bearish forecast and exposes costs', () => {
  const data = parseAlgorithms({ schema_version: 'core-chart-strategies-v1', status: 'AVAILABLE', reason: null, source: { symbol: 'sh600519', url: 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600519', retrieved_at: '2026-10-16T00:00:00Z', first: '2026-01-01', last: '2026-09-30', cutoff: '2026-09-30', bars: 180, raw_sha256: 'a'.repeat(64), bars_sha256: 'b'.repeat(64) }, strategies: ['均线趋势', '20日动量', '通道突破', '布林带回归'].map((label, index) => ({ id: String(index), label, parameters: { window: 20 }, status: 'COMPUTED', latest_state: 0, replay: [{ fee_bps: 10, price_return: -0.02, max_drawdown: -0.03 }] })), baseline: [], limitations: ['软件夹具，非真实行情'], forecast_validated: false, screenshot_identity_verified: false })
  render(<ChartAlgorithms analysis={data} />)
  expect(screen.getAllByText('规则下空仓')).toHaveLength(4)
  expect(screen.getByText('空仓不等于看跌；各类策略独立展示，不合成为涨跌概率。')).toBeInTheDocument()
  expect(screen.getAllByText('-2.00%')).toHaveLength(4)
  expect(screen.getAllByText('10bp')).toHaveLength(4)
})


it('accepts live QQ connection state and shows it without exposing a token', async () => {
  const originalFetch=globalThis.fetch
  vi.stubGlobal('fetch',vi.fn(async (url: string, options?: RequestInit)=>url.endsWith('/api/chart-settings') ? {ok:true,json:async()=>({...provider,qq_connected:true})} : originalFetch(url,options)))
  render(<ChartReports />)
  expect(await screen.findByText('QQ指定私聊已连接 · 交易权限关闭')).toBeInTheDocument()
  expect(parseProvider({...provider,qq_connected:true}).qq_connected).toBe(true)
  expect(()=>parseProvider({...provider,qq_connected:'yes'})).toThrow()
})
