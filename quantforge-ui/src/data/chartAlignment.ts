import type { ChartAnalysis } from './chartReports'

// Recheck old reports at display time without rewriting their stored evidence.
export function effectiveAlignment(analysis: ChartAnalysis) {
  const report = analysis.report, meta = analysis.request
  const unknown = { status: 'UNCONFIRMED', note: '截图证券、日期、周期或截止时间尚未完整核对，先确认信息，不给购入卖出结论。' }
  if (!report || !meta) return unknown
  const codes = new Set(report.recognized_symbol?.match(/(?<![0-9])[0-9]{6}(?![0-9])/g) || [])
  const expected = meta.symbol.match(/(?<![0-9])[0-9]{6}(?![0-9])/)?.[0]
  const period = report.recognized_period || ''
  const matches = [...period.matchAll(/(?<![0-9])([0-9]{4})[-/年]([0-9]{1,2})[-/月]([0-9]{1,2})日?(?![0-9])/g)]
  let dates = matches.map(m => `${m[1]}-${m[2].padStart(2, '0')}-${m[3].padStart(2, '0')}`)
  if (dates.some(d => !Number.isFinite(Date.parse(d)) || new Date(d).toISOString().slice(0, 10) !== d)) dates = []
  const clocks = [...period.matchAll(/(?<![0-9])([0-2][0-9]:[0-5][0-9])(?![0-9])/g)]
  const lastDate = matches.at(-1), clock = clocks.at(-1)
  const endClock = clock && lastDate && clock.index! >= lastDate.index! + lastDate[0].length ? clock[1] : null
  const hourly = meta.timeframe === '60m'
  const clockMatches = !hourly || endClock === (meta.period_end_time || '15:00')
  const datesMatch = dates.length > 0 && dates[0] === meta.period_start && dates.at(-1) === meta.period_end
  const datesConflict = dates.length > 0 && (dates.every(d => d > meta.period_end) || dates.every(d => d < meta.period_start) || dates.some((d, i) => i > 0 && d < dates[i - 1]))
  if ('adjustment' in meta) {
    if (['QFQ','HFQ'].includes(meta.adjustment || '') || ['QFQ','HFQ'].includes(report.recognized_adjustment || '')) return {status:'MISMATCH',note:'截图复权口径与当前不复权行情不一致，暂停综合价位参考。'}
    if (meta.adjustment !== 'RAW' || report.recognized_adjustment !== 'RAW') return {status:'UNCONFIRMED',note:'截图复权方式尚未核对；当前算法为不复权，暂停精确参考。'}
  }
  if (['UNREADABLE', 'NOT_A_CHART'].includes(report.image_quality) || (codes.size === 1 && expected && !codes.has(expected)) || datesConflict || (hourly && endClock !== null && !clockMatches) || (report.recognized_timeframe != null && report.recognized_timeframe !== (meta.timeframe || '1d'))) {
    return { status: 'MISMATCH', note: '截图无法辨认或证券、日期、周期、截止时间不一致，不给购入卖出结论。' }
  }
  if (codes.size !== 1 || !expected || !codes.has(expected) || report.recognized_timeframe !== (meta.timeframe || '1d') || !datesMatch || !clockMatches) return unknown
  if (analysis.image_alignment && analysis.image_alignment.status !== 'CONSISTENT') return analysis.image_alignment
  return { status: 'CONSISTENT', note: '模型辨认的代码、日期、周期及所需截止时间与填写一致；仍非交易所或独立数据认证。' }
}
