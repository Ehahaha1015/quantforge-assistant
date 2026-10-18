import type { ChartAnalysis } from '../data/chartReports'
import { effectiveAlignment } from '../data/chartAlignment'

export function ReportPlanReplay({analysis}: {analysis: ChartAnalysis}) {
  const replay=analysis.algorithm_analysis?.report_plan_replay
  if(!replay)return null
  const aligned=effectiveAlignment(analysis).status==='CONSISTENT'
  return <details className="chart-strategy-details"><summary>与报告共享规则的研究回放</summary>
    <p>预测期限 {replay.forecast_horizon_bars} 根日线 · 最长持仓观察 {replay.max_holding_bars} 根 · 每根完成后复评。</p>
    <p>从空仓与虚拟研究资金起步，不还原你填写的实际持仓；不代表实际成交收益。</p>
    {!aligned ? <p>截图及复权口径未核对，暂不展示这份报告的回放数值。</p> : <div className="chart-table-scroll" tabIndex={0} role="region" aria-label="报告规则回放表，可横向滚动"><table><caption>报告规则回放，含声明的费用及成交假设</caption><thead><tr><th scope="col">每侧综合摩擦</th><th scope="col">价格回放变化</th><th scope="col">最大回撤</th><th scope="col">模拟入场/完整退出</th><th scope="col">未能确认成交</th></tr></thead><tbody>{replay.summaries.map(row=><tr key={row.friction_bps_each_side}><td>{row.friction_bps_each_side}bp</td><td>{(Number(row.price_return_after_assumed_costs)*100).toFixed(2)}%</td><td>{(Number(row.max_drawdown)*100).toFixed(2)}%</td><td>{row.entries} / {row.closed_trades}</td><td>{row.blocked_attempts}</td></tr>)}</tbody></table></div>}
    {aligned && replay.summaries.every(x=>x.entries===0) && <p>该区间没有模拟入场；零回放变化不表示预测成功。</p>}
    <ul>{replay.limitations.map(x=><li key={x}>{x}</li>)}</ul>
  </details>
}
