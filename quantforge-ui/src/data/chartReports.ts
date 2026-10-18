import { chartApi as bindingsApi } from './chartApi'
import { parseConclusion, parseSynthesis, type Conclusion, type Synthesis } from './chartSynthesis'

export type Timeframe = '60m' | '1d' | '1w'
export const timeframeNames: Record<Timeframe,string> = { '60m': '60分钟', '1d': '日线', '1w': '周线' }
export const horizonText = (request: { timeframe?: Timeframe; horizon_bars?: number; horizon_sessions: number }) => `后续${request.horizon_bars ?? request.horizon_sessions}根${timeframeNames[request.timeframe || '1d']}K线`
export type TradePlan = { version: 'conditional-entry-exit-v1' | 'conditional-entry-exit-v2' | 'conditional-entry-exit-v3'; forecast_horizon_bars?: number; review_interval_bars?: number; entry_action?: string; position_action?: string; position_note?: string; levels_scope?: string; action: 'CONSIDER_ENTRY' | 'WAIT' | 'AVOID' | 'UNAVAILABLE' | 'EXIT_REVIEW' | 'HOLD_REVIEW'; basis: string; reasons: string[]; indicators: Record<string,string>; levels: { reference: string; stop_reference: string; take_profit_reference: string; rule: string } | null; exit_rules: string[]; max_observation_bars: number; held_bars: number; remaining_observation_bars: number; position_basis: 'USER_COST' | 'HYPOTHETICAL_REFERENCE'; timeframe: Timeframe; execution_constraint: string; as_of_bar: string }
export type Direction = 'BULLISH' | 'RANGE' | 'BEARISH' | 'UNCLEAR'
export type ChartStatus = 'WAITING_FOR_MODEL' | 'QUEUED' | 'RUNNING' | 'SUCCEEDED' | 'FAILED' | 'UNKNOWN'
type PriceReplay = { fee_bps: number; price_return: number; max_drawdown: number }
export type AlgorithmAnalysis = { schema_version: 'core-chart-strategies-v1' | 'core-chart-strategies-v2' | 'core-chart-strategies-v3' | 'core-chart-strategies-v4'; daily_research?: {policy: string; release_status: string; horizon_specific: boolean; features: Record<string,string|number|null>; evaluation_summary?: {development_assets:number;sealed_assets:number;sealed_windows:number;candidate:string;stock_promoted:false;etf_promoted:false;failed_gates:string[];note:string}}; report_plan_replay?: ReportPlanReplay; synthesis?: Synthesis | null; timeframe?: Timeframe; family_count?: number; trade_plan?: TradePlan | null; status: 'AVAILABLE' | 'UNAVAILABLE'; reason: string | null; source: { provider?: string; timeframe?: Timeframe; cutoff_at?: string; symbol: string; url: string; retrieved_at: string; first: string; last: string; bars: number; cutoff: string; raw_sha256: string; bars_sha256: string } | null; strategies: { id: string; label: string; parameters: Record<string, string | number>; entry_rule?: string; exit_rule?: string; family?: string; status: 'COMPUTED' | 'INSUFFICIENT_HISTORY'; latest_state: 0 | 1 | null; replay: PriceReplay[] }[]; baseline: PriceReplay[]; evaluation_first?: string; evaluation_last?: string; evaluation_bars?: number; replay_convention?: string; limitations: string[]; forecast_validated: false; screenshot_identity_verified: false }
export type ChartInput = { adjustment?: 'RAW' | 'QFQ' | 'HFQ' | 'UNKNOWN'; max_holding_bars?: 3 | 5 | 10 | 20 | null; review_interval_bars?: 1; include_context?: boolean; position_reference_id?: string | null; timeframe?: Timeframe; horizon_bars?: 1 | 4 | 5 | 20; period_end_time?: string; purchase_price?: string | null; held_bars?: number; symbol: string; display_name: string; period_start: string; period_end: string; horizon_sessions: 5 | 20; question: string; image_base64: string; channel: 'WEB' }
export type Report = { recognized_adjustment?: 'RAW' | 'QFQ' | 'HFQ' | 'UNKNOWN' | null; recognized_timeframe?: Timeframe | null; image_quality: 'READABLE' | 'PARTIAL' | 'UNREADABLE' | 'NOT_A_CHART'; recognized_symbol: string | null; recognized_period: string | null; summary: string; observed_trend: Direction; outlook: Direction; observations: { observation: string; image_region: string }[]; support_zones: string[]; resistance_zones: string[]; volume_comment: string; scenarios: { direction: Exclude<Direction, 'UNCLEAR'>; trigger: string; interpretation: string; invalidation: string }[]; uncertainties: string[] }
export type ChartSummary = { id: string; status: ChartStatus; request: Omit<ChartInput, 'image_base64'>; image: { sha256: string; bytes: number; width: number; height: number; mime: string }; created_at: string; updated_at: string }
export type ChartAnalysis = ChartSummary & { conclusion?: Conclusion | null; schema_version: 'chart-analysis-v1'; report: Report | null; algorithm_analysis?: AlgorithmAnalysis | null; image_alignment?: { status: 'MISMATCH' | 'CONSISTENT' | 'UNCONFIRMED'; note: string } | null; strategy_version: string; error: string | null; model_call: { model: string; reserved_cny: string } | null; live_authority: 'NONE'; forecast_validated: false }
export type ChartProvider = { schema_version: 'chart-provider-status-v1'; provider: 'DeepSeek'; model: string; configured: boolean; ready: boolean; max_cost_cny: string; reserved_cny: string; calls_used: number; calls_limit: number; qq_connected: boolean; strategy_version: string }
const record = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)
const states = ['WAITING_FOR_MODEL', 'QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'UNKNOWN']
const directions = ['BULLISH', 'RANGE', 'BEARISH', 'UNCLEAR']
const strings = (v: unknown): v is string[] => Array.isArray(v) && v.every(x => typeof x === 'string')
export const chartAsset = (id: string, kind: 'image' | 'report.md') => `${bindingsApi}/api/chart-analyses/${encodeURIComponent(id)}/${kind}`
export function parseAnalysis(v: unknown): ChartAnalysis {
  if (!record(v) || v.schema_version !== 'chart-analysis-v1' || v.live_authority !== 'NONE' || v.forecast_validated !== false || typeof v.id !== 'string' || !/^chart-[a-f0-9]{32}$/.test(v.id) || !states.includes(String(v.status)) || !record(v.request) || typeof v.request.symbol !== 'string' || !record(v.image) || typeof v.image.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(v.image.sha256)) throw new Error('截图任务或来源无法核验')
  if (v.report !== null) {
    const r = v.report
    if (!record(r) || typeof r.summary !== 'string' || !directions.includes(String(r.outlook)) || !directions.includes(String(r.observed_trend)) || !Array.isArray(r.observations) || !r.observations.every(x => record(x) && typeof x.observation === 'string' && typeof x.image_region === 'string') || !Array.isArray(r.scenarios) || !r.scenarios.every(x => record(x) && ['BULLISH', 'RANGE', 'BEARISH'].includes(String(x.direction)) && ['trigger', 'interpretation', 'invalidation'].every(k => typeof x[k] === 'string')) || !strings(r.support_zones) || !strings(r.resistance_zones) || !strings(r.uncertainties) || typeof r.volume_comment !== 'string') throw new Error('报告内容无法核验')
  }
  if (v.status === 'SUCCEEDED' && v.report === null) throw new Error('完成任务缺少报告')
  if (v.algorithm_analysis != null) parseAlgorithms(v.algorithm_analysis)
  if (v.image_alignment != null && (!record(v.image_alignment) || !['MISMATCH','CONSISTENT','UNCONFIRMED'].includes(String(v.image_alignment.status)) || typeof v.image_alignment.note !== 'string')) throw new Error('截图核对状态无法核验')
  if (v.conclusion != null) {
    const combined = parseConclusion(v.conclusion)
    if (!record(v.algorithm_analysis) || !['core-chart-strategies-v3','core-chart-strategies-v4'].includes(String(v.algorithm_analysis.schema_version))) throw new Error('综合结论缺少对应版本算法')
    if (combined.status === 'READY' && (!record(v.algorithm_analysis.synthesis) || combined.direction !== v.algorithm_analysis.synthesis.direction || !record(v.algorithm_analysis.trade_plan) || combined.action !== v.algorithm_analysis.trade_plan.action)) throw new Error('综合结论与算法不一致')
  }
  return v as ChartAnalysis
}
export function parseProvider(v: unknown): ChartProvider {
  if (!record(v) || 'api_key' in v || 'secret' in v || v.schema_version !== 'chart-provider-status-v1' || v.provider !== 'DeepSeek' || typeof v.configured !== 'boolean' || typeof v.ready !== 'boolean' || typeof v.max_cost_cny !== 'string' || typeof v.reserved_cny !== 'string' || !Number.isSafeInteger(v.calls_used) || typeof v.qq_connected !== 'boolean') throw new Error('识图服务状态无法核验')
  return v as ChartProvider
}
export class ChartRejected extends Error {}
export function parseAlgorithms(v: unknown): AlgorithmAnalysis {
  const replay = (r: unknown) => record(r) && [10, 30].includes(Number(r.fee_bps)) && typeof r.price_return === 'number' && Number.isFinite(r.price_return) && typeof r.max_drawdown === 'number' && Number.isFinite(r.max_drawdown)
  if (!record(v) || !['core-chart-strategies-v1', 'core-chart-strategies-v2', 'core-chart-strategies-v3', 'core-chart-strategies-v4'].includes(String(v.schema_version)) || !['AVAILABLE', 'UNAVAILABLE'].includes(String(v.status)) || v.forecast_validated !== false || v.screenshot_identity_verified !== false || !strings(v.limitations) || !Array.isArray(v.strategies) || !Array.isArray(v.baseline)) throw new Error('内置算法结果无法核验')
  if (v.status === 'AVAILABLE' && (!record(v.source) || typeof v.source.url !== 'string' || !(/^https:\/\/(?:web\.ifzq\.gtimg\.cn\/appstock\/app\/fqkline\/get|ifzq\.gtimg\.cn\/appstock\/app\/kline\/mkline)\?param=/.test(v.source.url) || v.source.provider === 'OFFLINE_SYNTHETIC' && v.source.url === 'demo://synthetic-bars') || !/^[a-f0-9]{64}$/.test(String(v.source.bars_sha256)) || !/^[a-f0-9]{64}$/.test(String(v.source.raw_sha256)) || v.strategies.length !== (v.schema_version === 'core-chart-strategies-v1' ? 4 : 8) || !v.strategies.every(x => record(x) && typeof x.id === 'string' && typeof x.label === 'string' && record(x.parameters) && ['COMPUTED', 'INSUFFICIENT_HISTORY'].includes(String(x.status)) && (x.status === 'COMPUTED' ? x.latest_state === 0 || x.latest_state === 1 : x.latest_state === null) && Array.isArray(x.replay) && x.replay.every(replay)) || !v.baseline.every(replay))) throw new Error('内置算法来源或数值无法核验')
  if (v.trade_plan != null) parseTradePlan(v.trade_plan)
  if (['core-chart-strategies-v2', 'core-chart-strategies-v3', 'core-chart-strategies-v4'].includes(String(v.schema_version)) && v.status === 'AVAILABLE' && (!['60m','1d','1w'].includes(String(v.timeframe)) || !record(v.source) || v.source.timeframe !== v.timeframe || !record(v.trade_plan) || v.trade_plan.timeframe !== v.timeframe || new Set(v.strategies.map(x=>record(x)?x.id:null)).size !== 8)) throw new Error('算法与行情周期不一致')
  if (['core-chart-strategies-v3','core-chart-strategies-v4'].includes(String(v.schema_version)) && v.status === 'AVAILABLE') { const combined = parseSynthesis(v.synthesis); if (combined.timeframe !== v.timeframe) throw new Error('综合证据周期不一致') }
  if (v.daily_research != null) {
    const d = v.daily_research
    if (!record(d) || !['BASELINE_V2','REGIME_ROUTER','REGIME_PERSISTENT'].includes(String(d.policy)) || !['NOT_PROMOTED','RESEARCH_VALIDATION_PENDING'].includes(String(d.release_status)) || typeof d.horizon_specific !== 'boolean' || !record(d.features) || !Object.values(d.features).every(x=>x===null || typeof x==='number' && Number.isFinite(x) || typeof x==='string' && Number.isFinite(Number(x)))) throw new Error('日线专项证据无法核验')
    if (d.evaluation_summary != null) {
      const e=d.evaluation_summary
      if (!record(e) || e.stock_promoted!==false || e.etf_promoted!==false || !['development_assets','sealed_assets','sealed_windows'].every(k=>Number.isInteger(e[k]) && Number(e[k])>0) || typeof e.candidate!=='string' || typeof e.note!=='string' || !strings(e.failed_gates)) throw new Error('封存检验状态无法核验')
    }
  }
  if (v.report_plan_replay != null) parseReportPlanReplay(v.report_plan_replay)
  return v as AlgorithmAnalysis
}
const refusals: Record<string, string> = {
  CHART_REVIEW_REQUIRES_ALIGNED_REPORT: '这份报告尚未完成证券、周期或不复权口径核对，暂不能进行最新行情复评。',
  CHART_POSITION_REFERENCE_INVALID: '关联的持仓报告与股票、周期、成本或截止时间不匹配，请重新选择。',
  BOUNDED_JSON_REQUIRED: '请求格式不正确，请重新提交。', OBJECT_REQUIRED: '请求格式不正确，请重新提交。',
  EXPLICIT_LOCAL_MODE_REQUIRED: '本机接口需要显式模式标识，请通过本机页面访问。',
  LOCAL_ORIGIN_REQUIRED: '只允许本机页面访问该接口。', SCREENSHOT_PRODUCT_ONLY: '当前部署只开放截图报告接口。',
  CHART_API_KEY_REQUIRED: '请先配置 DeepSeek API Key。', CHART_API_KEY_INVALID: '密钥格式有误，请检查空格或换行。',
  CHART_BUDGET_EXHAUSTED: '识图预算不足，请核查调用记录并调整总上限。', CHART_BUDGET_INVALID: '预算上限格式不正确，请填写不超过9999的金额。',
  CHART_BUDGET_CANNOT_RESET: '不能降低已授权上限或重置历史用量。', CHART_CONFIGURATION_BUSY: '报告正在生成，请完成后再调整配置。',
  CHART_CONFIGURATION_INVALID: '识图配置不完整，请同时确认密钥与预算。',
  CHART_IMAGE_SIZE_INVALID: '请上传不超过5MB的图片。', CHART_IMAGE_FORMAT_INVALID: '仅支持单张 PNG、JPEG 或 WebP 图片。',
  CHART_IMAGE_DIMENSIONS_INVALID: '图片尺寸需在160至6000像素之间，总像素不超过1200万。',
  CHART_IMAGE_ENCODING_INVALID: '图片数据不完整，请重新导出后上传。', CHART_IMAGE_INVALID: '图片无法读取，请换一张清晰的走势图。',
  CHART_IMAGE_INTEGRITY_FAILED: '原图校验未通过，请重新上传。',
  CHART_INPUT_OR_STORE_UNAVAILABLE: '请检查股票、日期、图片格式和大小。',
  CHART_REQUEST_INVALID: '股票、日期、周期或持仓参数格式有误，请检查后重新提交。',
  CHART_IDEMPOTENCY_KEY_REQUIRED: '这次提交缺少标识，请重新上传图片。',
  CHART_IDEMPOTENCY_CONFLICT: '同一提交标识的内容不一致，请重新上传。',
  CHART_CHANNEL_NOT_CONFIGURED: '网页入口不接受聊天通道身份。',
  CHART_ID_INVALID: '报告标识无效，请在分析记录中重新选择。', CHART_NOT_FOUND: '这份报告不存在或已被移除。',
  CHART_OR_REPORT_UNAVAILABLE: '暂时无法读取这份报告，请稍后重试。',
  CHART_START_REQUIRES_WAITING: '这份报告不在等待生成状态，无法再次发起。',
  CHART_PRIOR_CALL_REQUIRES_REVIEW: '同一张图曾出现结果未知的调用，请先核查服务商记录，再决定是否重试。',
  CHART_WORKSPACE_FULL: '报告数量已达上限（100份），请先清理本机数据目录再上传。',
  CHART_HISTORY_INTEGRITY_FAILED: '报告历史校验失败，请勿继续使用这条记录。',
  CHART_PROJECTION_INTEGRITY_FAILED: '任务记录校验失败，请勿继续使用这条记录。',
  CHART_MARKET_DATA_INTEGRITY_FAILED: '行情原始数据校验失败，本次不再给出算法结论。',
  CHART_SECRET_STORE_UNAVAILABLE: '本机密钥存储不可用，请重新配置识图服务。',
  CHART_SECRET_STORE_REDIRECTED: '本机密钥存储路径被重定向，已拒绝写入。',
  CHART_PROVIDER_REJECTED: '识图服务拒绝了本次请求，未产生可用报告。',
  CHART_PROVIDER_INVALID_REPORT: '模型返回内容未通过格式校验，本次不会自动重试。',
  CHART_PROVIDER_RESPONSE_TOO_LARGE: '模型返回内容超出上限，本次不会自动重试。',
  CHART_PROVIDER_EFFECT_UNKNOWN: '调用结果未知；为避免重复计费，不会自动重发。',
  CHART_ANALYSIS_FAILED_NO_AUTOMATIC_RETRY: '本次生成未完成，系统不会自动重试。',
}
export function refusalText(code: string): string { return refusals[code] || '' }
async function api(path: string, signal?: AbortSignal, payload?: object, key?: string, configuration = false): Promise<unknown> {
  const response = await fetch(`${bindingsApi}/api/chart-${path}`, { signal: signal || AbortSignal.timeout(20000), ...(payload === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-QF-Mode': configuration ? 'CONFIGURATION' : 'RESEARCH', ...(key ? { 'Idempotency-Key': key } : {}) }, body: JSON.stringify(payload) }) })
  let value: unknown
  try { value = await response.json() } catch {
    if (response.status === 413) throw new ChartRejected('上传图片过大。')
    throw new Error('响应无法核验，提交结果可能已保存。请核查同一次提交，避免重复生成。')
  }
  if (!response.ok) {
    const code = record(value) && typeof value.error === 'string' ? value.error : ''
    if (response.status >= 500 || code === 'CHART_INPUT_OR_STORE_UNAVAILABLE') throw new Error('服务暂时不可用，提交结果尚不明确。请核查同一次提交，避免重复生成。')
    throw new ChartRejected(refusalText(code) || (code ? `请求未完成（${code}）` : `请求未完成（HTTP ${response.status}）`))
  }
  return value
}
export async function readChartProvider(signal?: AbortSignal) { return parseProvider(await api('settings', signal)) }
export async function configureChartProvider(payload: { api_key?: string; max_cost_cny: string }) { return parseProvider(await api('settings', undefined, payload, undefined, true)) }
export async function readCharts(signal?: AbortSignal): Promise<ChartSummary[]> {
  const v = await api('analyses', signal)
  if (!record(v) || v.schema_version !== 'chart-list-v1' || !Array.isArray(v.items) || !v.items.every(x => record(x) && typeof x.id === 'string' && /^chart-[a-f0-9]{32}$/.test(x.id) && states.includes(String(x.status)) && record(x.request) && typeof x.request.symbol === 'string')) throw new Error('报告列表无法核验')
  return v.items as ChartSummary[]
}
export async function readChart(id: string, signal?: AbortSignal) { return parseAnalysis(await api('analyses/' + encodeURIComponent(id), signal)) }
export type ChartObservation = { schema_version: 'chart-observation-v1'; status: 'PENDING' | 'MATURED' | 'ORIGIN_MISSING' | 'ORIGIN_REVISED' | 'NO_FROZEN_TARGET'; horizon_bars?: number; observed_bars?: number; kind?: string; actual?: Direction | null; correct?: boolean | null; future_return?: string | null; first_reference_touch?: string; note: string; forecast_validated: false }
export async function readObservation(id: string): Promise<ChartObservation> {
  const v = await api('analyses/' + encodeURIComponent(id) + '/observation')
  if (!record(v) || v.schema_version !== 'chart-observation-v1' || v.forecast_validated !== false || !['PENDING','MATURED','ORIGIN_MISSING','ORIGIN_REVISED','NO_FROZEN_TARGET'].includes(String(v.status)) || typeof v.note !== 'string' || (v.future_return != null && (typeof v.future_return !== 'string' || !Number.isFinite(Number(v.future_return))))) throw new Error('观察结果无法核验')
  return v as ChartObservation
}
export async function createChart(payload: ChartInput, key: string) { return parseAnalysis(await api('analyses', undefined, payload, key)) }
export async function startChart(id: string) { return parseAnalysis(await api('analyses/' + encodeURIComponent(id) + '/start', undefined, { action: 'start' })) }

export function parseTradePlan(v: unknown): TradePlan {
  if (!record(v) || !['conditional-entry-exit-v1','conditional-entry-exit-v2','conditional-entry-exit-v3'].includes(String(v.version)) || !['CONSIDER_ENTRY','WAIT','AVOID','UNAVAILABLE','EXIT_REVIEW','HOLD_REVIEW'].includes(String(v.action)) || !strings(v.reasons) || !strings(v.exit_rules) || !record(v.indicators) || typeof v.basis !== 'string' || typeof v.execution_constraint !== 'string' || !['60m','1d','1w'].includes(String(v.timeframe)) || !Number.isInteger(v.max_observation_bars) || Number(v.max_observation_bars)<1 || Number(v.max_observation_bars)>20 || !Number.isInteger(v.remaining_observation_bars) || Number(v.remaining_observation_bars)<0) throw new Error('购入退出计划无法核验')
  if(!Number.isInteger(v.held_bars) || Number(v.held_bars)<0 || Number(v.remaining_observation_bars)>Number(v.max_observation_bars) || !['USER_COST','HYPOTHETICAL_REFERENCE'].includes(String(v.position_basis)) || typeof v.as_of_bar !== 'string' || !Object.values(v.indicators).every(x=>typeof x==='string' && Number.isFinite(Number(x)))) throw new Error('持仓或指标无法核验')
  const levels = v.levels
  if(levels !== null && (!record(levels) || !['reference','stop_reference','take_profit_reference'].every(k=>typeof levels[k] === 'string' && Number.isFinite(Number(levels[k])) && Number(levels[k]) > 0) || typeof levels.rule !== 'string')) throw new Error('参考价位无法核验')
  if (v.version==='conditional-entry-exit-v3' && (v.forecast_horizon_bars!==5 || v.review_interval_bars!==1 || v.timeframe!=='1d')) throw new Error('预测与持仓期限无法核验')
  return v as TradePlan
}

export type ReportPlanReplay = {schema_version:'report-plan-replay-v1'; policy:string; forecast_horizon_bars:5; max_holding_bars:number; review_interval_bars:1; scope:string; limitations:string[]; summaries:{friction_bps_each_side:number;price_return_after_assumed_costs:string;max_drawdown:string;closed_trades:number;open_quantity:number;blocked_attempts:number;entries:number;execution_qualified:false;artifact_sha256:string}[]}
export function parseReportPlanReplay(v: unknown): ReportPlanReplay {
  if (!record(v) || v.schema_version!=='report-plan-replay-v1' || v.forecast_horizon_bars!==5 || v.review_interval_bars!==1 || typeof v.policy!=='string' || typeof v.scope!=='string' || !strings(v.limitations) || ![3,5,10,20].includes(Number(v.max_holding_bars)) || !Array.isArray(v.summaries) || !v.summaries.every(x=>record(x) && [10,30].includes(Number(x.friction_bps_each_side)) && x.execution_qualified===false && ['price_return_after_assumed_costs','max_drawdown'].every(k=>typeof x[k]==='string' && Number.isFinite(Number(x[k]))) && ['closed_trades','open_quantity','blocked_attempts','entries'].every(k=>Number.isInteger(x[k]) && Number(x[k])>=0) && /^[a-f0-9]{64}$/.test(String(x.artifact_sha256)))) throw new Error('报告计划回放无法核验')
  return v as ReportPlanReplay
}

export type ChartReview = {schema_version:'chart-review-v1';status:'UNCHANGED'|'CHANGED'|'DATA_UNAVAILABLE'|'NO_NEW_BAR';job_id:string;original_direction:Direction;direction:Direction;original_action:string;action:string;original_as_of:string;latest_as_of:string|null;held_bars:number|null;levels:TradePlan['levels'];reasons:string[];note:string;forecast_validated:false;model_calls:0}
export async function reviewChart(id:string):Promise<ChartReview> {
  const v=await api('analyses/'+encodeURIComponent(id)+'/review',undefined,{action:'review'})
  if(!record(v)||v.schema_version!=='chart-review-v1'||v.job_id!==id||v.forecast_validated!==false||v.model_calls!==0||!['UNCHANGED','CHANGED','DATA_UNAVAILABLE','NO_NEW_BAR'].includes(String(v.status))||!directions.includes(String(v.direction))||!directions.includes(String(v.original_direction))||!strings(v.reasons)||typeof v.note!=='string'||typeof v.original_as_of!=='string'||(v.latest_as_of!==null&&typeof v.latest_as_of!=='string')) throw new Error('最新行情复评无法核验')
  if(v.status==='DATA_UNAVAILABLE'&&v.levels!==null) throw new Error('不完整复评不能展示精确参考')
  return v as ChartReview
}
export type ForwardStatus={schema_version:'daily-forward-status-v1';status:'ACTIVE'|'DISABLED'|'PAUSED_CODE_CHANGED'|'PAUSED_PROCESSING_FAILED'|'PAUSED_CALENDAR_UNVERIFIED'|'PAUSED_STORAGE_LIMIT';frozen:number;matured:number;pending:number;missed_days:number;failed_sources:number;model_calls:0;forecast_validated:false;note:string}
export async function readForwardStatus():Promise<ForwardStatus>{
  const response=await fetch(`${bindingsApi}/api/chart-forward`)
  if(!response.ok)throw new Error('前向记录暂不可用')
  const v:unknown=await response.json()
  if(!record(v)||v.schema_version!=='daily-forward-status-v1'||!['ACTIVE','DISABLED','PAUSED_CODE_CHANGED','PAUSED_PROCESSING_FAILED','PAUSED_CALENDAR_UNVERIFIED','PAUSED_STORAGE_LIMIT'].includes(String(v.status))||v.forecast_validated!==false||v.model_calls!==0||!['frozen','matured','pending','missed_days','failed_sources'].every(k=>Number.isSafeInteger(v[k])&&Number(v[k])>=0)||typeof v.note!=='string')throw new Error('前向记录无法核验')
  return v as ForwardStatus
}
