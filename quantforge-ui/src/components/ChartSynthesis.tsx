import type { ChartAnalysis } from '../data/chartReports'
import { timeframeNames } from '../data/chartReports'
import { effectiveAlignment } from '../data/chartAlignment'

const names = { BULLISH: '偏强', RANGE: '震荡', BEARISH: '偏弱', UNCLEAR: '证据不足' }
const relationships: Record<string,string> = { ALIGNED: '同向', CONFLICT: '不同周期存在反向证据', MIXED: '尚未形成同向确认', MISSING: '较长周期行情缺失', UNCONFIRMED:'较长周期数据有效，方向证据不足', NEUTRAL:'较长周期中性', SINGLE_PERIOD: '单周期分析' }
const risks: Record<string,string> = { HIGH_VOLATILITY: '波动偏高', VOLUME_UNAVAILABLE: '量能不可用', ZERO_VOLATILITY: '波动尺度不足', POSSIBLE_PRICE_DISCONTINUITY: '价格可能存在跳变，需核对公司行动', INSUFFICIENT_HISTORY: '预热历史不足' }
const regimes: Record<string,string> = { TREND: '趋势状态', RANGE: '区间状态', HIGH_VOLATILITY: '高波动状态', INSUFFICIENT: '历史不足' }

export function ChartSynthesis({analysis}: {analysis: ChartAnalysis}) {
  const combined = analysis.conclusion
  if (!combined) return null
  const alignment = effectiveAlignment(analysis)
  const ready = combined.status === 'READY' && alignment.status === 'CONSISTENT'
  return <section className="chart-section chart-synthesis" aria-label="综合策略结论">
    <span className="chart-kicker">8组策略 · 三类证据 · 固定规则综合</span>
    <h3>综合走势与参考动作</h3>
    <p className="chart-synthesis-headline">{ready ? combined.headline : '先核对截图，当前无法形成有效综合结论。'}</p>
    <p>{alignment.note}</p>
    {combined.uncertainty_note && <p className="chart-form-hint">{combined.uncertainty_note}</p>}
    {analysis.algorithm_analysis?.daily_research && <p className="chart-form-hint">日线未来5根专项 · 当前采用{analysis.algorithm_analysis.daily_research.policy === 'BASELINE_V2' ? '保守规则对照' : '已声明的状态协调规则'}。预测期限、最长持仓观察期和每根K线复评分别设置；研究结果不等于交易优势。</p>}
    {analysis.algorithm_analysis?.daily_research?.evaluation_summary && <p className="chart-form-hint">{analysis.algorithm_analysis.daily_research.evaluation_summary.note} 未通过：{analysis.algorithm_analysis.daily_research.evaluation_summary.failed_gates.join("；")}。</p>}
    <div className="chart-synthesis-metrics"><span>有效证据 {combined.coverage.computed} / {combined.coverage.total}</span><span>{regimes[combined.market_state] || '状态待核对'}</span><span>{relationships[combined.cross_timeframe_status] || '周期背景待核对'}</span></div>
    {ready && <>
      <div className="chart-synthesis-columns">{combined.groups.map(group=><article key={group.id}><h4>{group.label}</h4><strong>{names[group.direction]}{group.internal_conflict ? ' · 组内分歧' : ''}</strong><p>{group.members.map(id=>analysis.algorithm_analysis?.strategies.find(x=>x.id===id)?.label || '规则待核对').join('、')}</p></article>)}</div>
      <div className="chart-synthesis-columns"><div><h4>支持依据</h4><ul>{combined.supporting_evidence.map(x=><li key={x}>{x}</li>)}</ul></div><div><h4>反向证据与不足</h4><ul>{combined.counter_evidence.map(x=><li key={x}>{x}</li>)}</ul></div></div>
      <h4>不同周期分别看</h4><ul>{combined.timeframes.map(x=><li key={x.timeframe}>{timeframeNames[x.timeframe]}：{names[x.direction]} · 最后完成K线 {x.last_bar || '缺失'}{x.reason ? ` · ${x.reason}` : ''}</li>)}</ul>
      <ul>{combined.reasons.map(x=><li key={x}>{x}</li>)}</ul>
      {combined.risk_flags.length > 0 && <p className="chart-plan-constraint">{combined.risk_flags.map(x=>risks[x] || x).join('；')}</p>}
      <h3>未来走势 · 三种条件性情景</h3><div className="chart-scenarios">{combined.scenarios.map(x=><article key={x.direction} className={'chart-scenario '+x.direction.toLowerCase()}><h4>{names[x.direction]}</h4><span>触发条件</span><p>{x.trigger}</p><span>情景推演</span><p>{x.interpretation}</p><span>失效条件</span><p>{x.invalidation}</p></article>)}</div>
    </>}
    <p className="chart-form-hint">{combined.validation_note}</p>
  </section>
}
