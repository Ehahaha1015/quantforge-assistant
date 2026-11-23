import { useEffect, useRef, useState } from 'react'
import { askReport, compareReports, readBrief, type Answer, type Brief, type Claim, type Comparison, type Evidence } from '../data/chartAssistant'
import type { ChartAnalysis, ChartSummary } from '../data/chartReports'

function Claims({ items, prefix }: { items: Claim[]; prefix: string }) {
  return items.length ? <ul className="assistant-claims">{items.map((c, i) => <li key={i}><span>{c.text}</span><span className="assistant-citations">{c.evidence_ids.map(id => <a key={id} href={`#${prefix}-${id}`} onClick={event => { const target = document.getElementById(`${prefix}-${id}`); if (!target) return; event.preventDefault(); let element: HTMLElement | null = target; while (element) { if (element instanceof HTMLDetailsElement) element.open = true; element = element.parentElement }; target.scrollIntoView?.({ behavior: 'smooth', block: 'center' }); target.querySelector('summary')?.focus() }}>依据 {id}</a>)}</span></li>)}</ul> : <p className="chart-form-hint">尚无可确认的独立证据。</p>
}
const fieldNames: Record<string, string> = { recognized_symbol:'图中辨认的证券', recognized_period:'图中辨认的日期', recognized_timeframe:'图中辨认的周期', recognized_adjustment:'图中辨认的复权', image_quality:'图像可读性', symbol: '证券', timeframe: 'K线周期', adjustment: '复权口径', period_start: '区间开始', period_end: '区间截止', period_end_time: '截止时间', question: '重点问题', provider: '来源', last: '最后完整K线', first: '最早K线', retrieved_at: '取数时间', bars: '历史根数', direction: '规则方向', internal_conflict: '组内分歧', action: '参考动作', as_of_bar: '计算时点', reasons: '过滤原因', entry_reasons: '入场过滤原因', exit_rules: '退出与复评条件', execution_constraint: '成交限制', position_note: '持仓口径', basis: '适用边界', levels: '条件参考线', indicators: '计算指标', reference: '成本或假设参考', stop_reference: '估算止损参考', take_profit_reference: '估算止盈参考', rule: '计算规则', reference_close: '归档收盘参考', sma20: '20根均线', sma60: '60根均线', atr14_sma: '14根平均真实波幅', volume_ratio_prior20: '相对前20根均量', rsi14_wilder: 'RSI（14）', macd_dif: 'MACD DIF', macd_dea: 'MACD DEA', max_observation_bars: '最长观察根数', held_bars: '已持有根数', remaining_observation_bars: '剩余观察根数' }
const valueNames: Record<string, string> = { READABLE:'可辨认', PARTIAL:'部分可辨认', UNREADABLE:'不可辨认', NOT_A_CHART:'未识别为走势图', BULLISH: '偏强', BEARISH: '偏弱', RANGE: '震荡', UNCLEAR: '证据不足', WAIT: '等待确认', AVOID: '风险过滤未通过', UNAVAILABLE: '资料不足', CONSIDER_ENTRY: '条件满足时考虑购入', EXIT_REVIEW: '退出复核', HOLD_REVIEW: '按条件观察持仓', RAW: '不复权', RAW_UNADJUSTED: '不复权', UNKNOWN: '未确认', '1d': '日线', '1w': '周线', '60m': '60分钟', OFFLINE_SYNTHETIC: '离线合成样例', TENCENT_PUBLIC_HTTPS: '腾讯公开行情（非交易所认证）' }
function EvidenceValue({ value, depth = 0 }: { value: unknown; depth?: number }) {
  if (value === null) return <span>未提供</span>
  if (Array.isArray(value)) return value.length ? <ul>{value.map((v, i) => <li key={i}><EvidenceValue value={v} depth={depth + 1} /></li>)}</ul> : <span>无已记录项</span>
  if (typeof value === 'object') {
    const record = value as Record<string, unknown>
    if (record.schema_version === 'chart-analysis-v1') return <><EvidenceValue value={record.request} /><p>策略版本：{String(record.strategy_version)}</p></>
    const fields = Object.entries(record).filter(([key]) => key in fieldNames)
    return fields.length && depth < 3 ? <dl className="assistant-evidence-fields">{fields.map(([key, v]) => <div key={key}><dt>{fieldNames[key]}</dt><dd><EvidenceValue value={v} depth={depth + 1} /></dd></div>)}</dl> : <span>完整记录可在下方技术详情中核查。</span>
  }
  return <span>{typeof value === 'boolean' ? value ? '是' : '否' : valueNames[String(value)] || String(value)}</span>
}
function Sources({ evidence, prefix }: { evidence: Evidence[]; prefix: string }) {
  return <div className="assistant-sources">{evidence.map(e => <details key={e.id} id={`${prefix}-${e.id}`}><summary>{e.id} · {e.label}{e.kind === 'MODEL_OBSERVATION' ? ' · 模型观察，待核对' : ''}</summary><p>{e.note}</p><EvidenceValue value={e.value} /><details><summary>技术详情与原始字段</summary><code>{e.path}</code><pre>{JSON.stringify(e.value, null, 2)}</pre></details></details>)}</div>
}

export function ReportAssistant({ analysis, items }: { analysis: ChartAnalysis; items: ChartSummary[] }) {
  const [brief, setBrief] = useState<Brief | null>(null)
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)
  const [question, setQuestion] = useState('')
  const [answers, setAnswers] = useState<Answer[]>([])
  const [questionError, setQuestionError] = useState('')
  const [busy, setBusy] = useState(false)
  const [previous, setPrevious] = useState('')
  const [comparison, setComparison] = useState<Comparison | null>(null)
  const [comparisonError, setComparisonError] = useState('')
  const [comparing, setComparing] = useState(false)
  const generation = useRef(0)
  const version = `${analysis.id}:${analysis.updated_at}`
  useEffect(() => {
    const controller = new AbortController()
    generation.current++
    setBrief(null); setError(''); setAnswers([]); setQuestionError(''); setQuestion('')
    setPrevious(''); setComparison(null); setComparisonError(''); setBusy(false); setComparing(false)
    readBrief(analysis.id, controller.signal).then(setBrief).catch(e => { if (!controller.signal.aborted) setError((e as Error).message) })
    return () => { generation.current++; controller.abort() }
  }, [version, retry, analysis.id])
  const ask = async (text: string) => {
    if (busy || !text.trim() || !brief) return
    const current = generation.current
    setBusy(true); setQuestionError('')
    try {
      const result = await askReport(analysis.id, text.trim())
      if (current !== generation.current) return
      if (result.snapshot_sha256 !== brief.snapshot_sha256) throw new Error('报告版本已有变化，请重新读取证据后再提问。')
      setAnswers(old => [...old.slice(-5), result]); setQuestion('')
    } catch (e) { if (current === generation.current) setQuestionError((e as Error).message) }
    finally { if (current === generation.current) setBusy(false) }
  }
  const compare = async () => {
    if (!previous || comparing || !brief) return
    const current = generation.current
    setComparing(true); setComparison(null); setComparisonError('')
    try {
      const result = await compareReports(analysis.id, previous)
      if (current !== generation.current) return
      if (result.current_sha256 !== brief.snapshot_sha256) throw new Error('当前报告版本已有变化，请重新读取。')
      setComparison(result)
    } catch (e) { if (current === generation.current) setComparisonError((e as Error).message) }
    finally { if (current === generation.current) setComparing(false) }
  }
  const candidates = items.filter(i => i.id !== analysis.id && i.request.symbol.match(/\d{6}/)?.[0] === analysis.request.symbol.match(/\d{6}/)?.[0] && (i.request.timeframe || '1d') === (analysis.request.timeframe || '1d') && (i.request.adjustment || 'UNKNOWN') === (analysis.request.adjustment || 'UNKNOWN') && (i.request.horizon_bars || i.request.horizon_sessions) === (analysis.request.horizon_bars || analysis.request.horizon_sessions))
  const prefix = `brief-${analysis.id}`
  return <section className="report-assistant chart-section" aria-label="报告证据助手">
    <div className="assistant-heading"><div><span className="chart-kicker">读懂依据 · 留住疑问</span><h3>这份报告，先看这几件事</h3></div><span className="assistant-readonly">只读解释 · 不消耗模型额度</span></div>
    {!brief && !error && <p role="status">正在核对归档证据…</p>}
    {error && <div role="alert"><p>{error}</p><button className="button" onClick={() => setRetry(n => n + 1)}>重新读取报告证据</button></div>}
    {brief && <>
      <div className={`assistant-status ${brief.status === 'READY' ? 'ready' : 'blocked'}`}><strong>{brief.status_label}</strong><p>归档分析时点：{brief.as_of || '尚未确认'}{brief.offline ? ' · 离线合成样例' : ''}</p><p>{brief.scope_note}</p></div>
      <p className="assistant-headline">{brief.headline}</p>
      <div className="assistant-overview"><div><h4>支持判断的依据</h4><Claims items={brief.supporting} prefix={prefix} /></div><div><h4>反向证据与分歧</h4><Claims items={brief.opposing} prefix={prefix} /></div><div><h4>什么变化后需要复评？</h4><Claims items={brief.next_checks} prefix={prefix} /></div></div>
      <details className="assistant-missing"><summary>哪些信息还不能确认？（{brief.missing.length}项）</summary><Claims items={brief.missing} prefix={prefix} /></details>
      <details className="assistant-evidence-index"><summary>核查引用与归档来源</summary><Sources evidence={brief.evidence} prefix={prefix} /><p className="chart-form-hint">快照 SHA256：{brief.snapshot_sha256}</p></details>
      <div className="assistant-question"><h4>围绕这份报告继续问</h4><p>选择问题或输入关键词，查看这份报告已有的依据。新问题独立解释，不沿用上一问的假设。</p>
        {brief.focus_question && !brief.focus_question.startsWith('demo:') && <button className="button" type="button" disabled={busy} onClick={() => void ask(brief.focus_question)}>回答我的重点问题：{brief.focus_question}</button>}
        <div className="assistant-prompts">{['为什么偏强却要等待？', '有哪些反向证据？', '是否确认放量突破？', '什么条件下需要退出复评？', '还需要补充什么资料？'].map(q => <button key={q} className="button" type="button" disabled={busy} onClick={() => void ask(q)}>{q}</button>)}</div>
        <form onSubmit={e => { e.preventDefault(); void ask(question) }}><label className="chart-field">询问这份报告<textarea maxLength={600} rows={2} value={question} onChange={e => setQuestion(e.target.value)} placeholder="例如：为什么还需要等待？" /></label><button className="button primary" disabled={busy || !question.trim()}>{busy ? '正在整理依据…' : '查看已有证据的回答'}</button></form>
        {questionError && <p role="alert">{questionError}</p>}
        <div aria-live="polite">{answers.map((result, i) => <article className="assistant-answer" key={i}><h4>{result.question}</h4><span className="chart-form-hint">{result.status === 'OUT_OF_SCOPE' ? '超出归档证据范围' : result.status === 'INSUFFICIENT' ? '资料不足，无法确认' : '基于归档证据的解释'}</span><Claims items={result.claims} prefix={`answer-${analysis.id}-${i}`} /><details><summary>核查这次回答的依据</summary><Sources evidence={result.evidence} prefix={`answer-${analysis.id}-${i}`} /></details><p className="chart-form-hint">{result.note}</p></article>)}</div>
      </div>
      <div className="assistant-compare"><h4>与另一份报告对照</h4><p>仅比较相同证券、周期、复权和观察期限的归档报告。</p>{candidates.length ? <><label className="chart-field">选择对照报告<select value={previous} disabled={comparing} onChange={e => { setPrevious(e.target.value); setComparison(null); setComparisonError('') }}><option value="">请选择</option>{candidates.map(i => <option key={i.id} value={i.id}>{i.request.period_end} · {i.id.slice(-8)}</option>)}</select></label><button className="button" disabled={!previous || comparing} onClick={() => void compare()}>{comparing ? '正在比较…' : '比较已归档报告'}</button></> : <p className="chart-form-hint">暂时没有相同口径的其他报告。后续报告保存后可在这里对照。</p>}
        {comparisonError && <p role="alert">{comparisonError}</p>}
        {comparison && <div aria-live="polite"><Claims items={comparison.changes} prefix={`compare-${analysis.id}`} /><Sources evidence={comparison.evidence} prefix={`compare-${analysis.id}`} /><p className="chart-form-hint">{comparison.note}</p></div>}
      </div>
    </>}
  </section>
}
