import { useEffect, useState } from 'react'
import { reviewChart, type ChartAnalysis, type ChartReview as Review } from '../data/chartReports'
import { effectiveAlignment } from '../data/chartAlignment'

const states = { UNCHANGED:'原规则条件仍一致', CHANGED:'原条件已有变化，请重新复核', DATA_UNAVAILABLE:'资料不足，暂停精确参考', NO_NEW_BAR:'尚无新的完整K线' }
const directions = { BULLISH:'偏强', BEARISH:'偏弱', RANGE:'震荡倾向', UNCLEAR:'证据不足' }
export function ChartReview({analysis}:{analysis:ChartAnalysis}) {
  const [result,setResult]=useState<Review|null>(null)
  const [error,setError]=useState('')
  const [busy,setBusy]=useState(false)
  useEffect(()=>{setResult(null);setError('')},[analysis.id])
  if(analysis.status!=='SUCCEEDED'||analysis.conclusion?.status!=='READY')return null
  if(effectiveAlignment(analysis).status!=='CONSISTENT'||analysis.request.adjustment!=='RAW'||analysis.report?.recognized_adjustment!=='RAW')return <section className="chart-section"><h3>最新行情复评</h3><p>此报告尚未明确核验不复权口径，暂不提供最新价位复评。</p></section>
  const check=async()=>{setBusy(true);setError('');try{setResult(await reviewChart(analysis.id))}catch(e){setError((e as Error).message)}finally{setBusy(false)}}
  return <section className="chart-section" aria-label="最新行情复评"><h3>这份报告的条件现在还成立吗？</h3>
    <p>仅在点击后读取最新完整行情，不重新识图；复评结果另存，原报告不变。</p>
    <button className="button" type="button" disabled={busy} onClick={()=>void check()}>{busy?'正在复评…':'用最新行情复评'}</button>
    {error&&<p role="alert">{error}</p>}
    {result&&<div aria-live="polite"><h4>{states[result.status]}</h4><p>原判断：{directions[result.original_direction]} · 最新规则倾向：{directions[result.direction]}</p>
      <p>原时点 {result.original_as_of} · 最新完整K线 {result.latest_as_of||'不可用'}</p>
      {result.held_bars!=null&&<p>按完整K线计数，累计观察 {result.held_bars} 根。</p>}
      <ul>{result.reasons.map(x=><li key={x}>{x}</li>)}</ul><p>{result.note}</p></div>}
  </section>
}
