import { timeframeNames, type AlgorithmAnalysis } from '../data/chartReports'
import type { Evidence } from '../data/chartSynthesis'

const pct = (value: number) => `${(value * 100).toFixed(2)}%`
const evidenceNames = { BULLISH: '偏强', BEARISH: '偏弱', RANGE: '尚未确认单一方向', UNCLEAR: '无法判断' }
const transitionNames: Record<string,string> = { ENTRY: '新出现研究入场', EXIT: '研究退出', CONTINUING: '研究持仓延续', WAITING: '等待规则触发', UNAVAILABLE: '历史不足' }
function RuleEvidence({evidence}: {evidence: Evidence | undefined}) {
  if (!evidence) return null
  return <><p>方向证据：{evidenceNames[evidence.direction]}</p><p>{transitionNames[evidence.transition] || '状态待核对'}</p><small>{evidence.reason}</small></>
}

export function ChartAlgorithms({ analysis }: { analysis: AlgorithmAnalysis | null | undefined }) {
  return <section className="chart-section chart-algorithms" aria-label="内置算法分析">
    <h3>内置算法分析</h3>
    {!analysis ? <p>此任务尚无算法结果；旧版报告不会被追溯改写。</p> : analysis.status !== 'AVAILABLE' ? <p>{analysis.reason} 未使用模型补造算法结果。</p> : <>
      <p>{analysis.source!.symbol} · {timeframeNames[analysis.timeframe || '1d']} · 不复权 · 截至 {analysis.source!.last} · {analysis.source!.bars} 根完整K线</p>
      <p>按已确认代码查询；尚未独立核验截图中的证券和复权口径。</p>
      <div className="chart-algorithm-cards">{analysis.strategies.map(item => <article key={item.id}>
        <h4>{item.label}</h4>
        <strong>{item.status !== 'COMPUTED' ? '历史不足，未计算' : item.latest_state === 1 ? '规则下研究持仓' : '规则下空仓'}</strong>
        <RuleEvidence evidence={analysis.synthesis?.evidence.find(x=>x.id === item.id)} />
        <p>{Object.entries(item.parameters).map(([key, value]) => `${key}: ${value}`).join(' · ')}</p>{item.entry_rule && <p>入场：{item.entry_rule}</p>}{item.exit_rule && <p>退出：{item.exit_rule}</p>}
      </article>)}</div>
      <p>空仓不等于看跌；各类策略独立展示，不合成为涨跌概率。</p>
      <details className="chart-algorithm-replay"><summary>历史价格回放与持有基线</summary>
        <p>{analysis.evaluation_first} — {analysis.evaluation_last} · {analysis.evaluation_bars} 根{timeframeNames[analysis.timeframe || '1d']}K线</p>
        <p>{analysis.replay_convention}</p>
        <div className="chart-table-scroll"><table><caption>价格序列模拟，非含分红总收益或未来预测</caption><thead><tr><th scope="col">规则</th><th scope="col">摩擦</th><th scope="col">价格变化</th><th scope="col">最大回撤</th></tr></thead><tbody>
          {analysis.strategies.flatMap(item => item.replay.map(row => <tr key={`${item.id}-${row.fee_bps}`}><th scope="row">{item.label}</th><td>{row.fee_bps}bp</td><td>{pct(row.price_return)}</td><td>{pct(row.max_drawdown)}</td></tr>))}
          {analysis.baseline.map(row => <tr key={`baseline-${row.fee_bps}`}><th scope="row">持有基线</th><td>{row.fee_bps}bp</td><td>{pct(row.price_return)}</td><td>{pct(row.max_drawdown)}</td></tr>)}
        </tbody></table></div>
        {!analysis.strategies.some(item => item.replay.length > 0) && <p>预热历史或区间长度不足，策略回放未计算。</p>}
      </details>
      <details className="chart-source"><summary>行情来源与算法局限</summary><p>{analysis.source!.provider === 'OFFLINE_SYNTHETIC' ? <span>离线合成行情（非真实市场数据）</span> : <a href={analysis.source!.url} target="_blank" rel="noreferrer">腾讯公开行情原接口</a>} · 抓取于 {analysis.source!.retrieved_at}</p><code>原响应 SHA256 {analysis.source!.raw_sha256}</code><code>标准行情 SHA256 {analysis.source!.bars_sha256}</code><ul>{analysis.limitations.map(note => <li key={note}>{note}</li>)}</ul></details>
    </>}
  </section>
}
