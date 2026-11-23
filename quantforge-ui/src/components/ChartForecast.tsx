import { useEffect, useId, useState } from 'react'
import type { ChartAnalysis, Direction } from '../data/chartReports'
import { timeframeNames } from '../data/chartReports'
import { effectiveAlignment } from '../data/chartAlignment'
import { readVisualization, type ChartVisualization } from '../data/chartVisualization'
import '../chartForecast.css'

const names: Record<Direction, string> = { BULLISH: '偏强', RANGE: '震荡', BEARISH: '偏弱', UNCLEAR: '证据不足' }
const colors = { BULLISH: '#f6a08a', RANGE: '#dcc27d', BEARISH: '#6dc9b6', UNCLEAR: '#b1c9dc' }
const num = (v: string | number) => Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 4 })
const price = (v: string) => Number(v)
const W = 1000, H = 410, left = 74, right = 24, top = 34, bottom = 48, futureStart = 660

export function ForecastPlot({ view, analysis }: { view: ChartVisualization; analysis: ChartAnalysis }) {
  const uid = useId().replace(/:/g, '')
  const [windowBars, setWindowBars] = useState(40)
  const [showMeans, setShowMeans] = useState(true)
  const [showLevels, setShowLevels] = useState(true)
  const [scene, setScene] = useState<Direction | 'ALL'>('ALL')
  const [hovered, setHovered] = useState<number | null>(null)
  const [step, setStep] = useState(view.horizon_bars)
  const candles = view.candles.slice(-windowBars)
  const stride = (futureStart - left - 22) / Math.max(1, candles.length - 1)
  const x = (index: number) => left + index * stride
  const fx = (index: number) => futureStart + index / view.horizon_bars * (W - right - futureStart)
  const values = candles.flatMap(c => [price(c.high), price(c.low), ...(showMeans ? [c.sma20, c.sma60].filter((v): v is string => v !== null).map(price) : [])])
  const levels = view.levels
  if (levels && showLevels) values.push(price(levels.reference), price(levels.stop_reference), price(levels.take_profit_reference))
  const final = view.boundaries.at(-1)!
  values.push(price(final.upper), price(final.lower))
  const min = Math.min(...values), max = Math.max(...values), span = Math.max(max - min, max * .01)
  const floor = Math.max(0, min - span * .14), ceiling = max + span * .14
  const y = (v: string | number) => top + (ceiling - Number(v)) / (ceiling - floor) * (H - top - bottom)
  const originX = x(candles.length - 1)
  const line = (key: 'upper' | 'lower') => view.boundaries.map((b, i) => `${i ? 'L' : 'M'}${fx(b.step)},${y(b[key])}`).join(' ')
  const upper = line('upper'), lower = line('lower')
  const midPolygon = [...view.boundaries.map(b => `${fx(b.step)},${y(b.upper)}`), ...[...view.boundaries].reverse().map(b => `${fx(b.step)},${y(b.lower)}`)].join(' ')
  const band = view.boundaries[step]
  const selectedCandle = hovered === null ? null : candles[hovered]
  const selectedScenario = scene === 'ALL' || scene === 'UNCLEAR' ? null : (view.view_mode === 'BACKGROUND' ? [] : analysis.conclusion?.scenarios || []).find(s => s.direction === scene)
  const opacity = (direction: Direction) => scene === 'ALL' || scene === direction ? 1 : .18
  const means = (key: 'sma20' | 'sma60') => candles.map((c, i) => c[key] === null ? '' : `${i && candles[i - 1][key] !== null ? 'L' : 'M'}${x(i)},${y(c[key]!)}`).join(' ')
  return <>
    <div className="forecast-controls"><label>历史窗口<select value={windowBars} onChange={e => { setWindowBars(Number(e.target.value)); setHovered(null) }}>{[20, 40, 80, 120].map(n => <option key={n} value={n}>最近 {n} 根</option>)}</select></label><label><input type="checkbox" checked={showMeans} onChange={e => setShowMeans(e.target.checked)} />20 / 60 根均线</label>{levels && <label><input type="checkbox" checked={showLevels} onChange={e => setShowLevels(e.target.checked)} />条件参考线</label>}</div>
    <div className="forecast-legend"><span><i style={{ background: colors.BULLISH }} />收涨K线 / 偏强分区</span><span><i style={{ background: colors.BEARISH }} />收跌K线 / 偏弱分区</span><span><i style={{ background: colors.RANGE }} />震荡分区</span>{showMeans && <><span><i style={{ background: '#dbbc71' }} />MA20</span><span><i style={{ background: '#9aa8df' }} />MA60</span></>}<span className="forecast-dashed">虚线：判定边界，非预计轨迹</span></div>
    <div className="forecast-svg-container"><svg viewBox={`0 0 ${W} ${H}`} role="group" aria-roledescription="交互式行情图" aria-labelledby={`${uid}-title ${uid}-desc`}>
      <title id={`${uid}-title`}>{view.symbol}历史K线与未来{view.horizon_bars}根条件分区</title><desc id={`${uid}-desc`}>左侧为归档历史行情，右侧为用ATR阈值划分的未来方向条件。没有预测K线、目标价格或概率。可悬停或用键盘聚焦历史K线核对数值。</desc>
      <defs><clipPath id={`${uid}-clip`}><rect x={left - 8} y={top} width={W - left - right + 8} height={H - top - bottom} /></clipPath></defs>
      <rect x={futureStart} y={top} width={W - right - futureStart} height={H - top - bottom} fill="#172a32" />
      {Array.from({ length: 5 }, (_, i) => { const value = floor + i / 4 * (ceiling - floor); return <g key={i}><line x1={left} x2={W - right} y1={y(value)} y2={y(value)} stroke="#31413f" strokeDasharray="3 7" /><text x={left - 10} y={y(value) + 4} textAnchor="end" fill="#a4bbb3" fontSize={11}>{num(value)}</text></g> })}
      <text x={left} y={18} fill="#c6d9d0" fontSize={12}>归档历史 · {timeframeNames[view.timeframe]}</text><text x={futureStart + 12} y={18} fill="#b9d1de" fontSize={12}>未来 {view.horizon_bars} 根 · 条件分区</text>
      <g clipPath={`url(#${uid}-clip)`}>
        <path d={`${upper} L${W - right},${top} L${futureStart},${top} Z`} fill={colors.BULLISH} fillOpacity={.075 * opacity('BULLISH')} />
        <polygon points={midPolygon} fill={colors.RANGE} fillOpacity={.13 * opacity('RANGE')} />
        <path d={`${lower} L${W - right},${H - bottom} L${futureStart},${H - bottom} Z`} fill={colors.BEARISH} fillOpacity={.075 * opacity('BEARISH')} />
        <path d={upper} fill="none" stroke={colors.BULLISH} strokeWidth={2} strokeDasharray="6 5" opacity={opacity('BULLISH')} /><path d={lower} fill="none" stroke={colors.BEARISH} strokeWidth={2} strokeDasharray="6 5" opacity={opacity('BEARISH')} />
        {candles.map((c, i) => { const rising = price(c.close) >= price(c.open), color = rising ? colors.BULLISH : colors.BEARISH; return <g key={c.at}><line x1={x(i)} x2={x(i)} y1={y(c.high)} y2={y(c.low)} stroke={color} /><rect x={x(i) - Math.max(1.5, Math.min(5, stride * .3))} width={Math.max(3, Math.min(10, stride * .6))} y={Math.min(y(c.open), y(c.close))} height={Math.max(1.4, Math.abs(y(c.open) - y(c.close)))} fill={color} /></g> })}
        {showMeans && <><path d={means('sma20')} fill="none" stroke="#dbbc71" strokeWidth={1.5} /><path d={means('sma60')} fill="none" stroke="#9aa8df" strokeWidth={1.5} /></>}
        <line x1={originX} x2={W - right} y1={y(view.reference_close!)} y2={y(view.reference_close!)} stroke="#b7cad1" strokeDasharray="2 5" />
        {showLevels && levels && [['stop_reference', '止损参考', '#f29c9b'], ['take_profit_reference', '止盈参考', '#8ed4a6'], ['reference', '成本/假设参考', '#b3c0d2']].map(([key, label, color]) => <g key={key}><line x1={left} x2={W - right} y1={y(levels[key as keyof typeof levels])} y2={y(levels[key as keyof typeof levels])} stroke={color} opacity={.7} strokeDasharray="8 6" /><text x={left + 8} y={y(levels[key as keyof typeof levels]) - 5} fill={color} fontSize={10}>{label} {num(levels[key as keyof typeof levels])}</text></g>)}
        {selectedCandle && <><line x1={x(hovered!)} x2={x(hovered!)} y1={top} y2={H - bottom} stroke="#b9d0c3" opacity={.5} /><circle cx={x(hovered!)} cy={y(selectedCandle.close)} r={4} fill="#e2ede6" /></>}
        <line x1={fx(step)} x2={fx(step)} y1={top} y2={H - bottom} stroke="#bdd3df" opacity={.35} />
        {candles.map((c, i) => <rect className="forecast-hit" key={`hit-${c.at}`} x={x(i) - stride / 2} width={Math.max(3, stride)} y={top} height={H - top - bottom} fill="transparent" tabIndex={0} role="button" aria-label={`${c.label}，开盘${num(c.open)}，最高${num(c.high)}，最低${num(c.low)}，收盘${num(c.close)}`} onMouseEnter={() => setHovered(i)} onFocus={() => setHovered(i)} onClick={() => setHovered(i)} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setHovered(i) } }} />)}
        {view.boundaries.slice(1).map(b => <rect className="forecast-hit" key={b.step} x={fx(b.step) - (W - right - futureStart) / view.horizon_bars / 2} width={(W - right - futureStart) / view.horizon_bars} y={top} height={H - top - bottom} fill="transparent" tabIndex={0} role="button" aria-label={`查看后续第${b.step}根的判定边界`} onMouseEnter={() => { setHovered(null); setStep(b.step) }} onFocus={() => { setHovered(null); setStep(b.step) }} onClick={() => { setHovered(null); setStep(b.step) }} onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setHovered(null); setStep(b.step) } }} />)}
      </g>
      <line x1={futureStart} x2={futureStart} y1={top} y2={H - bottom} stroke="#7595a5" strokeDasharray="4 5" />
      <text x={left} y={H - 24} fill="#99b1a6" fontSize={10}>{candles[0].label}</text><text x={originX} y={H - 24} textAnchor="end" fill="#99b1a6" fontSize={10}>{view.as_of}</text><text x={W - right} y={H - 24} textAnchor="end" fill="#b9d1de" fontSize={10}>+{view.horizon_bars}根完整K线</text>
      <text x={futureStart + 12} y={H - 7} fill="#8fa8b6" fontSize={10}>未来根数，不推断具体交易日期</text>
    </svg></div>
    <label className="forecast-step">查看未来根数 <input type="range" min={1} max={view.horizon_bars} step={1} value={step} onChange={e => { setStep(Number(e.target.value)); setHovered(null) }} />第 {step} / {view.horizon_bars} 根</label>
    <div className="forecast-readout" aria-live="polite">{selectedCandle ? <><strong>{selectedCandle.label}</strong><span>开 {num(selectedCandle.open)}</span><span>高 {num(selectedCandle.high)}</span><span>低 {num(selectedCandle.low)}</span><span>收 {num(selectedCandle.close)}</span>{showMeans && <><span>MA20 {selectedCandle.sma20 ? num(selectedCandle.sma20) : '历史不足'}</span><span>MA60 {selectedCandle.sma60 ? num(selectedCandle.sma60) : '历史不足'}</span></>}</> : <><strong>后续第 {step} 根的分类边界</strong><span>偏强：高于 {num(band.upper)}</span><span>震荡：{num(band.lower)} — {num(band.upper)}</span><span>偏弱：低于 {num(band.lower)}</span></>}</div>
    <div className="forecast-scenes" aria-label="查看条件情景">{(['ALL', 'BULLISH', 'RANGE', 'BEARISH'] as const).map(value => <button key={value} className="button" aria-pressed={scene === value} onClick={() => { setScene(value); setHovered(null) }}>{value === 'ALL' ? '查看全部情景' : `查看${names[value]}情景`}</button>)}</div>
    {selectedScenario && <div className="forecast-scenario-detail"><strong style={{ color: colors[selectedScenario.direction] }}>{names[selectedScenario.direction]}情景</strong><p>触发条件：{selectedScenario.trigger}</p><p>{selectedScenario.interpretation}</p><p>失效条件：{selectedScenario.invalidation}</p></div>}
    {view.view_mode !== 'BACKGROUND' && <div className="forecast-evidence">{analysis.conclusion?.groups.map(group => <div key={group.id}><span>{group.label}</span><strong style={{ color: colors[group.direction] }}>{names[group.direction]}{group.internal_conflict ? ' · 有分歧' : ''}</strong></div>)}</div>}
    <details className="forecast-method"><summary>这张图是怎样算出来的？</summary><p>{view.formula}。ATR14 来自这份报告：{num(view.atr14!)}；归档收盘参考：{num(view.reference_close!)}。</p><p>{view.interpretation}中间根数只演示同一阈值公式，不代表生成了逐根预测。历史均线仅使用当根及之前的归档收盘价。</p>{levels && <p>条件参考线：{levels.rule} 参考线可能基于填写成本，不能当成当前市场价格。</p>}<p>行情 SHA256：{view.source_sha256}</p><p>标准行情 SHA256：{view.bars_sha256}</p></details>
  </>
}

function ImageScenarios({ analysis }: { analysis: ChartAnalysis }) {
  const [chosen, setChosen] = useState<Direction>('BULLISH')
  const report = analysis.report!
  const scenario = report.scenarios.find(s => s.direction === chosen)
  const id = useId().replace(/:/g, '')
  return <div className="image-scenario-view"><p className="forecast-summary">图像走势：<strong>{names[report.observed_trend]}</strong> · 图像情景示意</p><p className="forecast-disclaimer">先看图中结构与条件分支。下图不设价格轴，不从截图补造OHLC或精确未来价位。</p>
    <svg viewBox="0 0 700 220" role="img" aria-labelledby={id}><title id={id}>基于截图观察的三种条件情景示意，不是价格轨迹</title><rect x="0" y="0" width="700" height="220" rx="10" fill="#142326" /><circle cx="80" cy="110" r="7" fill="#c2dcd2" /><text x="30" y="150" fill="#c2dcd2" fontSize="13">图中当前状态</text>{(['BULLISH','RANGE','BEARISH'] as const).map((direction,i)=> { const endY=45+i*65; return <g key={direction} opacity={chosen===direction?1:.3}><path d={`M90,110 C260,110 330,${endY} 540,${endY}`} stroke={colors[direction]} strokeWidth={chosen===direction?3:2} strokeDasharray="7 6" fill="none" /><circle cx="540" cy={endY} r="5" fill={colors[direction]}/><text x="558" y={endY+5} fill={colors[direction]} fontSize="14">{names[direction]}情景</text></g>})}</svg>
    <div className="forecast-scenes">{(['BULLISH','RANGE','BEARISH'] as const).map(direction=><button type="button" className="button" key={direction} aria-pressed={chosen===direction} onClick={()=>setChosen(direction)}>查看{names[direction]}情景</button>)}</div>
    {scenario && <div className="forecast-scenario-detail"><p>触发：{scenario.trigger}</p><p>{scenario.interpretation}</p><p>失效：{scenario.invalidation}</p></div>}
    <p className="forecast-summary">{report.summary}</p><ul className="assistant-claims">{report.observations.map((fact,i)=><li key={i}>{fact.observation}（{fact.image_region}）</li>)}</ul>
  </div>
}

export function ChartForecast({ analysis, progressive = false }: { analysis: ChartAnalysis; progressive?: boolean }) {
  const [view, setView] = useState<ChartVisualization | null>(null)
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  const aligned = effectiveAlignment(analysis).status === 'CONSISTENT'
  const imageReadable = !!analysis.report && ['READABLE','PARTIAL'].includes(analysis.report.image_quality)
  useEffect(() => {
    const controller = new AbortController()
    setView(null); setError('')
    if (!imageReadable || (!progressive && !aligned) || analysis.status !== 'SUCCEEDED') return () => controller.abort()
    readVisualization(analysis.id, controller.signal, true).then(result => {
      if (controller.signal.aborted) return
      if (result.symbol !== analysis.request.symbol || result.timeframe !== (analysis.request.timeframe || '1d') || result.horizon_bars !== (analysis.request.horizon_bars || (analysis.request.timeframe === '60m' ? 4 : analysis.request.timeframe === '1w' ? 1 : analysis.request.horizon_sessions)) || analysis.analysis_guard && result.snapshot_sha256 !== analysis.analysis_guard.snapshot_sha256 || result.bars_sha256 !== (analysis.algorithm_analysis?.source?.bars_sha256 || null) && result.status === 'AVAILABLE') throw new Error('图表与这份报告的来源或版本不一致。')
      setView(result)
    }).catch(e => { if (!controller.signal.aborted) setError((e as Error).message) })
    return () => controller.abort()
  }, [analysis.id, analysis.updated_at, aligned, progressive, imageReadable, attempt, analysis.status, analysis.request.symbol, analysis.request.timeframe, analysis.request.horizon_bars, analysis.request.horizon_sessions, analysis.analysis_guard?.snapshot_sha256, analysis.algorithm_analysis?.source?.bars_sha256])
  if (!analysis.report) return null
  if (!progressive && !aligned) return <section className="chart-forecast chart-section" aria-label="走势与条件情景可视化"><p className="forecast-empty">先核对截图与行情，暂不绘制价格情景。{effectiveAlignment(analysis).note}</p></section>
  return <section className="chart-forecast chart-section" aria-label="走势与条件情景可视化"><div className="forecast-heading"><div><span className="chart-kicker">算法证据 · 图形推演</span><h3>把走势与未来条件放到一张图上</h3></div><span className="forecast-badge">条件情景 · 预测效果未验证</span></div>
    {!imageReadable ? <p className="forecast-empty">图片无法辨认，请换一张清晰图。</p> : error ? <><ImageScenarios analysis={analysis}/><div role="alert"><p>{error}</p><button className="button" onClick={() => setAttempt(n => n + 1)}>重新读取归档图表</button></div></> : !view ? <p role="status">正在重建归档图表，不调用模型或读取新行情…</p> : view.status !== 'AVAILABLE' ? <><ImageScenarios analysis={analysis}/><p className="chart-form-hint">行情背景暂不可用：{view.reason}</p></> : <>{view.view_mode === 'BACKGROUND' && <div className="forecast-scenario-detail"><strong>先展示行情背景，精确参考待核对</strong>{view.warnings?.map(note=><p key={note}>{note}</p>)}</div>}<p className="forecast-summary">截至 {view.as_of} · {view.view_mode === 'BACKGROUND' ? '同一证券不复权行情背景' : <>综合倾向：<strong>{names[view.direction]}</strong></>} · 后续 {view.horizon_bars} 根{timeframeNames[view.timeframe]}K线{view.offline ? ' · 合成演示数据' : ''}</p><p className="forecast-disclaimer">右侧展示满足什么涨跌幅才属于各类情景，不表示价格将沿虚线运行，不提供涨跌概率。</p><ForecastPlot key={`${analysis.id}-${view.snapshot_sha256}`} view={view} analysis={analysis} /></>}
  </section>
}
