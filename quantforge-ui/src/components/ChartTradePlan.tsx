import { timeframeNames, type ChartAnalysis } from '../data/chartReports'
import { effectiveAlignment } from '../data/chartAlignment'

const actions = { CONSIDER_ENTRY: '条件满足，可考虑购入', WAIT: '暂不购入，等待确认', AVOID: '暂不购入，风险过滤未通过', UNAVAILABLE: '信息不足，不给购入结论', EXIT_REVIEW: '已触发退出复核条件', HOLD_REVIEW: '继续观察持仓，按条件复评' }
export function ChartTradePlan({ analysis }: { analysis: ChartAnalysis }) {
  const plan = analysis.algorithm_analysis?.trade_plan
  if (!plan) return null
  const alignment = effectiveAlignment(analysis)
  const aligned = alignment.status === 'CONSISTENT'
  return <section className="chart-trade-plan" aria-label="购入与退出参考">
    <span className="chart-kicker">固定规则 · 条件性计划</span>
    <h3>{aligned ? actions[plan.action] : '先核对截图，暂不给购入或卖出结论'}</h3>
    <p>{alignment.note}</p>
    {plan.position_note && <p>{plan.position_note}</p>}
    {plan.forecast_horizon_bars && <p>预测期限 {plan.forecast_horizon_bars} 根 · 最长持仓观察 {plan.max_observation_bars} 根 · 每 {plan.review_interval_bars || 1} 根完整K线复评。</p>}
    <p>计算时点：{plan.as_of_bar} · {timeframeNames[plan.timeframe]} · {plan.position_basis === 'USER_COST' ? '按填写成本估算' : '未提供持仓，按假设入场参考价估算'}</p>
    {aligned && <ul>{plan.reasons.map(x => <li key={x}>{x}</li>)}</ul>}
    {aligned && plan.levels && <div className="chart-plan-levels">
      <article><span>{plan.position_basis === 'USER_COST' ? '填写的购入成本' : '假设入场参考'}</span><strong>{plan.levels.reference}</strong></article>
      <article><span>估算止损参考</span><strong>{plan.levels.stop_reference}</strong></article>
      <article><span>估算止盈参考</span><strong>{plan.levels.take_profit_reference}</strong></article>
    </div>}
    {aligned && <><p>{plan.levels?.rule}</p><h4>购入后多久复评、什么条件下退出？</h4><ul>{plan.exit_rules.map(x => <li key={x}>{x}</li>)}</ul><p>最长观察 {plan.max_observation_bars} 根完整{timeframeNames[plan.timeframe]}K线 · 已持有 {plan.held_bars} 根 · 剩余观察 {plan.remaining_observation_bars} 根。此上限是复评规则，不是最佳卖出时间。</p></>}
    <p className="chart-plan-constraint">{plan.execution_constraint}</p>
    <small>{plan.basis} 所有参考均针对图示截止时点，不是当前实时下单指令，也未经预测准确率验证。</small>
  </section>
}
