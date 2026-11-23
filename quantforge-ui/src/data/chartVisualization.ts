import { chartApi } from './chartApi'
import type { Direction, Timeframe } from './chartReports'

export type Candle = { at: string; label: string; open: string; high: string; low: string; close: string; sma20: string | null; sma60: string | null }
export type Boundary = { step: number; upper: string; reference: string; lower: string }
export type ChartVisualization = {
  schema_version: 'chart-visualization-v1'; job_id: string; snapshot_sha256: string
  status: 'AVAILABLE' | 'BLOCKED' | 'UNAVAILABLE'; reason: string | null; symbol: string; timeframe: Timeframe
  horizon_bars: number; direction: Direction; as_of: string | null; offline: boolean
  source_sha256: string | null; bars_sha256: string | null; candles: Candle[]; boundaries: Boundary[]
  levels: { reference: string; stop_reference: string; take_profit_reference: string; rule: string } | null
  atr14: string | null; reference_close: string | null; formula: string; interpretation: string
  model_calls: 0; market_fetches: 0; forecast_validated: false
}
const record = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)
const hash = (v: unknown) => typeof v === 'string' && /^[a-f0-9]{64}$/.test(v)
const price = (v: unknown): v is string => typeof v === 'string' && v.trim() !== '' && Number.isFinite(Number(v)) && Number(v) > 0 && Number(v) <= 1e12
const near = (a: number, b: number) => Math.abs(a - b) <= Math.max(1e-8, Math.abs(b) * 1e-9)

export function parseVisualization(v: unknown, id: string): ChartVisualization {
  if (!record(v) || v.schema_version !== 'chart-visualization-v1' || v.job_id !== id || !hash(v.snapshot_sha256) || v.model_calls !== 0 || v.market_fetches !== 0 || v.forecast_validated !== false || !['AVAILABLE', 'BLOCKED', 'UNAVAILABLE'].includes(String(v.status)) || !['BULLISH', 'RANGE', 'BEARISH', 'UNCLEAR'].includes(String(v.direction)) || !['60m', '1d', '1w'].includes(String(v.timeframe)) || !Number.isInteger(v.horizon_bars) || !({ '60m': [1, 4], '1d': [1, 5, 20], '1w': [1, 4] }[v.timeframe as Timeframe].includes(Number(v.horizon_bars))) || typeof v.symbol !== 'string' || typeof v.offline !== 'boolean' || typeof v.formula !== 'string' || typeof v.interpretation !== 'string' || !Array.isArray(v.candles) || !Array.isArray(v.boundaries)) throw new Error('图表身份或期限无法核验。')
  if (v.status !== 'AVAILABLE') {
    if (typeof v.reason !== 'string' || v.candles.length || v.boundaries.length || v.levels !== null || v.atr14 !== null || v.reference_close !== null) throw new Error('资料不足时不能展示价格图形。')
    return v as ChartVisualization
  }
  if (!hash(v.source_sha256) || !hash(v.bars_sha256) || typeof v.as_of !== 'string' || !price(v.atr14) || !price(v.reference_close) || v.candles.length < 2 || v.candles.length > 120 || v.boundaries.length !== Number(v.horizon_bars) + 1 || v.reason !== null) throw new Error('图表来源或波动尺度无法核验。')
  if (!v.candles.every((c, i, rows) => record(c) && typeof c.at === 'string' && Number.isFinite(Date.parse(c.at)) && typeof c.label === 'string' && ['open', 'high', 'low', 'close'].every(k => price(c[k])) && Number(c.low) <= Math.min(Number(c.open), Number(c.close)) && Number(c.high) >= Math.max(Number(c.open), Number(c.close)) && (c.sma20 === null || price(c.sma20)) && (c.sma60 === null || price(c.sma60)) && (i === 0 || Date.parse(c.at) > Date.parse(rows[i - 1].at)))) throw new Error('归档K线数值或时序异常。')
  const reference = Number(v.reference_close), atr = Number(v.atr14)
  if (!near(Number(v.candles.at(-1).close), reference) || v.candles.at(-1).label !== v.as_of) throw new Error('K线末值与报告参考价不一致。')
  if (!v.boundaries.every((b, i) => record(b) && b.step === i && ['upper', 'reference', 'lower'].every(k => price(b[k])) && near(Number(b.reference), reference) && near(Number(b.upper), reference + .5 * atr * Math.sqrt(i)) && near(Number(b.lower), reference - .5 * atr * Math.sqrt(i)))) throw new Error('情景边界偏离已有ATR阈值。')
  if (v.levels !== null && (!record(v.levels) || !['reference', 'stop_reference', 'take_profit_reference'].every(k => price((v.levels as Record<string, unknown>)[k])) || typeof v.levels.rule !== 'string')) throw new Error('条件参考线无法核验。')
  return v as ChartVisualization
}

export async function readVisualization(id: string, signal?: AbortSignal): Promise<ChartVisualization> {
  const bounded = signal ? AbortSignal.any([signal, AbortSignal.timeout(20000)]) : AbortSignal.timeout(20000)
  const response = await fetch(`${chartApi}/api/chart-analyses/${encodeURIComponent(id)}/visualization`, { signal: bounded })
  if (!response.ok) throw new Error('归档图表暂不可用，请重新读取；不会重新调用模型。')
  return parseVisualization(await response.json(), id)
}
