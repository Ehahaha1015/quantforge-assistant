import { useEffect, useState } from 'react'
import { readObservation, type ChartAnalysis, type ChartObservation as Observation } from '../data/chartReports'

const status = { PENDING: '观察期尚未结束', MATURED: '观察期结果已记录', ORIGIN_MISSING: '行情窗口未覆盖原始时点', ORIGIN_REVISED: '供应商原始时点价格有变化，暂停判定', NO_FROZEN_TARGET: '旧报告没有冻结的综合目标' }
const names = { BULLISH: '偏强', RANGE: '震荡', BEARISH: '偏弱', UNCLEAR: '未作判断' }
export function ChartObservation({analysis}: {analysis: ChartAnalysis}) {
  const [result,setResult] = useState<Observation | null>(null)
  const [error,setError] = useState('')
  const [busy,setBusy] = useState(false)
  useEffect(()=>{setResult(null);setError('')},[analysis.id])
  if (!analysis.conclusion) return null
  const check = async () => {
    setBusy(true);setError('')
    try {setResult(await readObservation(analysis.id))} catch(failure) {setError((failure as Error).message)} finally {setBusy(false)}
  }
  return <section className="chart-section" aria-label="观察期结果">
    <h3>报告到期后，看看实际走向</h3><p>主动检查后续完整K线，不调用识图模型；原报告保持不变。事后回看的报告单独标明。</p>
    <button className="button" type="button" disabled={busy} onClick={()=>void check()}>{busy ? '正在核查…' : '检查观察期结果'}</button>
    {error && <p role="alert">{error}</p>}
    {result && <div aria-live="polite"><h4>{status[result.status]}</h4>
      {result.observed_bars != null && <p>已观察 {result.observed_bars} / {result.horizon_bars} 根完整K线。</p>}
      {result.kind && <p>{result.kind === 'FORWARD_FROZEN' ? '后续行情开始前已冻结的报告' : result.kind === 'RETROSPECTIVE' ? '事后回看，不能计入真实前向预测成绩' : '尚待后续行情核对时间关系'}</p>}
      {result.actual && <p>后续收盘方向：{names[result.actual]} · {result.correct == null ? '原报告未形成方向判断' : result.correct ? '与原倾向一致' : '与原倾向不一致'}</p>}
      {result.future_return != null && <p>该观察区间收盘价格变化 {(Number(result.future_return)*100).toFixed(2)}%，不代表实际交易收益。</p>}
      {result.first_reference_touch === 'BOTH_AMBIGUOUS_STOP_FIRST' && <p>同根K线同时触及止盈与止损参考，先后不明，按止损优先保守记录；不视为实际成交。</p>}
      <p>{result.note}</p></div>}
  </section>
}
