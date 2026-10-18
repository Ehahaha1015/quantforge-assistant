import { ChartDemoNotice, ChartTimeline } from '../components/ChartDiagnostics'
import { useEffect, useRef, useState } from 'react'
import { ArrowRight, Check, Download, FileImage, ImageUp, KeyRound, LoaderCircle } from 'lucide-react'
import { Badge, PageHeading } from '../components/ui'
import { chartAsset, ChartRejected, configureChartProvider, createChart, readChart, readChartProvider, readCharts, refusalText, startChart, type ChartAnalysis, type ChartInput, type ChartProvider, type ChartSummary, type Direction, type Timeframe, horizonText, timeframeNames } from '../data/chartReports'
import '../chartReports.css'
import { ChartTradePlan } from '../components/ChartTradePlan'
import { ChartAlgorithms } from '../components/ChartAlgorithms'
import { ChartSynthesis } from '../components/ChartSynthesis'
import { ChartObservation } from '../components/ChartObservation'
import { ReportPlanReplay } from '../components/ReportPlanReplay'
import { ChartReview } from '../components/ChartReview'
import { ChartForwardStatus } from '../components/ChartForwardStatus'

const names = { WAITING_FOR_MODEL: '等待识图服务', QUEUED: '排队中', RUNNING: '正在分析', SUCCEEDED: '报告已生成', FAILED: '生成未完成', UNKNOWN: '调用结果待核查' }
const trends: Record<Direction, string> = { BULLISH: '偏强', RANGE: '震荡', BEARISH: '偏弱', UNCLEAR: '无法判断' }
const quality = { READABLE: '图像可读', PARTIAL: '部分信息不清晰', UNREADABLE: '图像无法辨认', NOT_A_CHART: '未识别为走势图' }
const dateText = (date: Date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
const monthBefore = () => { const date = new Date(); date.setDate(date.getDate() - 30); return dateText(date) }
const time = (v: string) => new Date(v).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
function readFile(file: File): Promise<string> {
  return new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(',')[1]); reader.onerror = () => reject(new Error('图片读取失败，请重新选择。')); reader.readAsDataURL(file) })
}

export function ChartReports() {
  const [provider, setProvider] = useState<ChartProvider | null>(null)
  const [items, setItems] = useState<ChartSummary[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [analysis, setAnalysis] = useState<ChartAnalysis | null>(null)
  const [file, setFile] = useState<File | null>(null)
  const [preview, setPreview] = useState('')
  const [symbol, setSymbol] = useState('')
  const [periodStart, setPeriodStart] = useState(monthBefore)
  const [periodEnd, setPeriodEnd] = useState(() => dateText(new Date()))
  const [horizon, setHorizon] = useState<1 | 4 | 5 | 20>(5)
  const [timeframe, setTimeframe] = useState<Timeframe>('1d')
  const [endTime, setEndTime] = useState('15:00')
  const [purchasePrice, setPurchasePrice] = useState('')
  const [heldBars, setHeldBars] = useState(0)
  const [adjustment,setAdjustment] = useState<'RAW'|'QFQ'|'HFQ'|'UNKNOWN'>('UNKNOWN')
  const [maxHolding,setMaxHolding] = useState<3|5|10|20>(5)
  const [includeContext, setIncludeContext] = useState(true)
  const [positionReference, setPositionReference] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const [question, setQuestion] = useState('分析所选区间和周期，说明购入或观望的条件、退出和复评规则，以及信息不足。')
  const [busy, setBusy] = useState(false)
  const [configBusy, setConfigBusy] = useState(false)
  const [apiKey, setApiKey] = useState('')
  const [budget, setBudget] = useState('')
  const [error, setError] = useState('')
  const [online, setOnline] = useState(false)
  const [notice, setNotice] = useState('')
  const pending = useRef<{ payload: ChartInput; key: string } | null>(null)
  const submitting = useRef(false)
  useEffect(() => () => { if (preview.startsWith('blob:')) URL.revokeObjectURL(preview) }, [preview])
  useEffect(() => {
    let active = true, timer: number
    const controller = new AbortController()
    const refresh = async () => {
      try {
        const [p, list, detail] = await Promise.all([readChartProvider(controller.signal), readCharts(controller.signal), selected ? readChart(selected, controller.signal) : Promise.resolve(null)])
        if (active) { setProvider(p); setItems(list); setOnline(true); if (detail) setAnalysis(detail) }
      } catch { if (active) setOnline(false) }
      finally { if (active) timer = window.setTimeout(refresh, 2000) }
    }
    void refresh()
    return () => { active = false; controller.abort(); window.clearTimeout(timer) }
  }, [selected])
  const chooseFile = (next: File | undefined) => {
    if (!next) return
    setError('')
    if (next.size > 5 * 1024 * 1024 || next.size === 0) { setError('请选择不超过5MB的图片。'); return }
    if (next.type && !['image/png', 'image/jpeg', 'image/webp'].includes(next.type)) { setError('请选择 PNG、JPEG 或 WebP 图片。'); return }
    setFile(next); setPreview(URL.createObjectURL(next)); setConfirmed(false)
  }
  const submit = async (reuse = false) => {
    if (submitting.current) return
    if (!reuse && (!file || !confirmed)) { setError('请先选择截图，并确认股票与时间范围。'); return }
    submitting.current = true; setBusy(true); setError(''); setNotice('')
    try {
      if (!reuse && file) pending.current = { key: crypto.randomUUID(), payload: { adjustment, max_holding_bars: timeframe==='1d' && horizon===5 ? maxHolding : null, review_interval_bars:1, include_context: includeContext, position_reference_id: positionReference || null, symbol: symbol.trim(), display_name: '', period_start: periodStart, period_end: periodEnd, horizon_sessions: horizon === 20 ? 20 : 5, timeframe, horizon_bars: horizon, period_end_time: endTime, purchase_price: purchasePrice || null, held_bars: heldBars, question, image_base64: await readFile(file), channel: 'WEB' } }
      if (!pending.current) return
      const value = await createChart(pending.current.payload, pending.current.key)
      pending.current = null; setSelected(value.id); setAnalysis(value); setItems(await readCharts())
      if (value.status === 'WAITING_FOR_MODEL') setNotice('截图已保存。配置识图服务后，可点击“生成这份报告”。')
    } catch (failure) { setError((failure as Error).message); if (failure instanceof ChartRejected) pending.current = null }
    finally { submitting.current = false; setBusy(false) }
  }
  const configure = async () => {
    setConfigBusy(true); setError(''); setNotice('')
    try { const result = await configureChartProvider({ ...(apiKey ? { api_key: apiKey.trim() } : {}), max_cost_cny: budget }); setProvider(result); setApiKey(''); setNotice('识图连接与总预算已保存。历史调用记录保留。') }
    catch (failure) { setError((failure as Error).message) }
    finally { setConfigBusy(false) }
  }
  const start = async () => {
    if (!analysis) return
    setBusy(true); setError('')
    try { setAnalysis(await startChart(analysis.id)); setItems(await readCharts()) }
    catch (failure) { setError((failure as Error).message) }
    finally { setBusy(false) }
  }
  const choose = (id: string) => { setSelected(id); setAnalysis(null); setNotice('') }
  const report = analysis?.report
  return <div className="chart-assistant">
    <PageHeading eyebrow="QUANTFORGE / CHART ASSISTANT" title="股票截图报告" description="选择60分钟、日线或周线，结合内置策略梳理依据与进退条件。" action={<Badge tone={online ? 'positive' : 'warning'}>{online ? '报告服务在线' : '正在连接报告服务'}</Badge>} />
    <div className="chart-intro"><div><span className="chart-kicker">从一张截图开始</span><h2>看清这段走势，写清每一种可能。</h2><p>图像观察、条件性推演与信息不足分别呈现。不会把单张截图解读成确定的涨跌结论。</p></div><div className="chart-intro-steps"><span><b>01</b> 上传与确认</span><span><b>02</b> 模型解读</span><span><b>03</b> 查看与下载</span></div></div>
    {error && <div role="alert" className="chart-message chart-error">{error}</div>}
    {notice && <div role="status" className="chart-message">{notice}</div>}
    <ChartDemoNotice /><div className="chart-grid"><aside className="chart-input-panel" aria-label="新分析与模型配置">
      <form onSubmit={event => { event.preventDefault(); void submit() }}>
        <div className="chart-panel-title"><ImageUp size={18} /><h2>新建分析</h2></div>
        <label className={'chart-upload' + (preview ? ' has-image' : '')} onDragOver={event => event.preventDefault()} onDrop={event => { event.preventDefault(); if (!busy && !pending.current) chooseFile(event.dataTransfer.files[0]) }}>
          {preview ? <img src={preview} alt="待分析的股票走势图预览" /> : <><ImageUp size={34} /><strong>点击选择或拖入走势图</strong><span>PNG / JPEG / WebP · 最大5MB</span><small>尽量保留股票名称、日期、价格轴和成交量。</small></>}
          <input aria-label="上传股票走势图" type="file" accept="image/png,image/jpeg,image/webp" disabled={busy || !!pending.current} onChange={event => chooseFile(event.target.files?.[0])} />
        </label>
        {file && <div className="chart-file-name"><FileImage size={14} />{file.name}<span>{(file.size / 1024).toFixed(0)}KB</span></div>}
        <label className="chart-field">股票代码或名称<input value={symbol} maxLength={40} required placeholder="例如：600519 / 贵州茅台" disabled={busy || !!pending.current} onChange={event => { setSymbol(event.target.value); setPositionReference(''); setConfirmed(false) }} /></label>
        <div className="chart-date-row"><label className="chart-field">图中开始日期<input type="date" value={periodStart} required disabled={busy || !!pending.current} onChange={event => { setPeriodStart(event.target.value); setConfirmed(false) }} /></label><label className="chart-field">图中结束日期<input type="date" value={periodEnd} min={periodStart} required disabled={busy || !!pending.current} onChange={event => { setPeriodEnd(event.target.value); setConfirmed(false) }} /></label></div>
        <label className="chart-field">图中K线周期<select value={timeframe} disabled={busy || !!pending.current} onChange={event => { const next = event.target.value as Timeframe; setTimeframe(next); setPositionReference(''); setPurchasePrice(''); setHeldBars(0); setHorizon(next === '60m' ? 4 : next === '1w' ? 1 : 5); if (next === '1w') { const day = new Date(); day.setFullYear(day.getFullYear()-1); setPeriodStart(dateText(day)) } else setPeriodStart(monthBefore()); setConfirmed(false) }}><option value="60m">60分钟</option><option value="1d">日线（默认）</option><option value="1w">周线</option></select></label>
        <p className="chart-form-hint">仅计算已收盘K线。周线不使用未结束的当前周；60分钟行情仅覆盖供应商最近640根。</p>
        {timeframe === '60m' && <label className="chart-field">图中截止时间（北京时间）<input type="time" value={endTime} required disabled={busy || !!pending.current} onChange={event => { setEndTime(event.target.value); setConfirmed(false) }} /></label>}
        <label className="chart-confirm"><input type="checkbox" checked={includeContext} disabled={busy || !!pending.current} onChange={event=>setIncludeContext(event.target.checked)} /><span>参考较长周期背景（缺失或冲突时等待确认）</span></label>
        <label className="chart-field">后续走势观察范围<select value={horizon} disabled={busy || !!pending.current} onChange={event => {setHorizon(Number(event.target.value) as 1 | 4 | 5 | 20);setPurchasePrice('');setHeldBars(0);setPositionReference('')}}>{(timeframe === '1d' ? [1,5,20] : [1,4]).map(n => <option key={n} value={n}>后续{n}{timeframe === '60m' ? '根60分钟K线' : timeframe === '1w' ? '周' : '个交易日'}</option>)}</select></label>
        <label className="chart-field">图中复权方式<select value={adjustment} disabled={busy || !!pending.current} onChange={event=>{setAdjustment(event.target.value as typeof adjustment);setConfirmed(false)}}><option value="UNKNOWN">无法确认</option><option value="RAW">不复权</option><option value="QFQ">前复权</option><option value="HFQ">后复权</option></select></label>
        <p className="chart-form-hint">当前算法使用不复权行情。复权未知或不一致时仅保留图像说明，不展示精确参考。</p>
        {timeframe==='1d' && horizon===5 && <label className="chart-field">最长持仓观察（完整日线根数）<select value={maxHolding} disabled={busy || !!pending.current} onChange={event=>setMaxHolding(Number(event.target.value) as 3|5|10|20)}>{[3,5,10,20].map(n=><option key={n} value={n}>{n} 根；每根完成后复评</option>)}</select></label>}
        <label className="chart-field">沿用持仓参考（可选）<select value={positionReference} disabled={busy || !!pending.current} onChange={event=>{ const id=event.target.value; setPositionReference(id); const parent=items.find(x=>x.id===id); if(parent?.request.purchase_price){setPurchasePrice(parent.request.purchase_price);setHeldBars(parent.request.held_bars || 0)} }}><option value="">本次独立估算</option>{items.filter(x=>x.status==='SUCCEEDED' && x.request.purchase_price && (x.request.timeframe || '1d')===timeframe && x.request.symbol.match(/[0-9]{6}/)?.[0]===symbol.match(/[0-9]{6}/)?.[0] && symbol.match(/[0-9]{6}/)).map(x=><option key={x.id} value={x.id}>{x.request.symbol} · {x.request.period_end} · {x.id.slice(-8)}</option>)}</select></label>
        <details className="chart-position"><summary>已持仓？填写成本用于退出参考（可选）</summary><label className="chart-field">购入成本<input type="number" min="0.0001" max="99999999" step="0.0001" value={purchasePrice} disabled={busy || !!pending.current} onChange={event => { setPositionReference(''); setPurchasePrice(event.target.value); if (!event.target.value) setHeldBars(0) }} /></label><label className="chart-field">已持有的完整K线数<input type="number" min="0" max="10000" step="1" value={heldBars} disabled={!purchasePrice || busy || !!pending.current} onChange={event => setHeldBars(Number(event.target.value))} /></label><p className="chart-form-hint">根数按所选周期计算；这里只按填写值估算，不读取账户。最长观察是复评上限，不是最佳卖点。</p></details>
        <label className="chart-field">希望重点分析什么？<textarea value={question} maxLength={600} rows={3} disabled={busy || !!pending.current} onChange={event => setQuestion(event.target.value)} /></label>
        <label className="chart-confirm"><input type="checkbox" checked={confirmed} disabled={busy || !!pending.current} onChange={event => setConfirmed(event.target.checked)} /><span>我已确认股票、K线周期和截止时间与截图一致。</span></label>
        <button className="button primary chart-submit" type="submit" disabled={busy || !online || !file || !confirmed || !!pending.current}>{busy ? <LoaderCircle size={16} className="chart-spin" /> : <ArrowRight size={16} />}{provider?.ready ? '上传并生成报告' : '保存截图，稍后生成'}</button>
        {pending.current && <button type="button" className="button chart-submit" disabled={busy} onClick={() => { void submit(true) }}>核查同一次提交</button>}
      </form>
      <details className="chart-config" open={!provider?.configured}>
        <summary><KeyRound size={15} />识图服务与预算<Badge tone={provider?.configured ? 'positive' : 'warning'}>{provider?.configured ? '已配置' : '待配置'}</Badge></summary>
        <p>DeepSeek V4.1 Flash · 密钥仅提交到本机后端，不会显示在报告中。</p>
        <form onSubmit={event => { event.preventDefault(); void configure() }}><label className="chart-field">API Key<input type="password" autoComplete="off" value={apiKey} placeholder={provider?.configured ? '留空保留现有密钥' : '填写 DeepSeek API Key'} onChange={event => setApiKey(event.target.value)} /></label><label className="chart-field">累计预算上限（人民币）<input type="number" min="0" max="9999" step="0.01" required value={budget} placeholder={provider?.max_cost_cny || '例如10'} onChange={event => setBudget(event.target.value)} /></label><button className="button" disabled={configBusy || !online} type="submit">{configBusy ? '正在保存…' : '保存连接与预算'}</button></form>
        <p className="chart-budget">调用尝试 {provider?.calls_used || 0} 次 · 保守预留 ¥{Number(provider?.reserved_cny || 0).toFixed(3)} / 上限 ¥{provider?.max_cost_cny || '0'}</p><small>预留金额为保守估算，以服务商账单为准；修改预算不会清零历史记录。</small>
      </details>
    </aside><section className="chart-results" aria-label="截图分析结果">
      {analysis ? <>
        <div className="chart-result-header"><div><span className="chart-kicker">{analysis.request.symbol}</span><h2>{names[analysis.status]}</h2><p>{analysis.request.period_start} — {analysis.request.period_end} · {horizonText(analysis.request)}</p></div><Badge tone={analysis.status === 'SUCCEEDED' ? 'positive' : 'warning'}>{names[analysis.status]}</Badge></div>
        {!report && <div className="chart-empty-result">{analysis.status === 'RUNNING' || analysis.status === 'QUEUED' ? <LoaderCircle size={36} className="chart-spin" /> : <FileImage size={36} />}<h3>{analysis.status === 'UNKNOWN' ? '调用结果未知，已停止自动重发' : analysis.status === 'FAILED' ? '本次报告未完成' : analysis.status === 'WAITING_FOR_MODEL' ? '截图已保存，等待识图服务' : '正在整理图中信息与趋势情景'}</h3><p>{analysis.status === 'UNKNOWN' ? '请先核查服务商请求与账单，再决定如何处理。' : analysis.status === 'FAILED' ? '可能是服务拒绝、图片数据异常或报告格式未通过检查。本次不会自动重试。' : analysis.status === 'WAITING_FOR_MODEL' ? '配置 DeepSeek API Key 与预算后，即可生成这份报告。' : '任务在后台执行，离开页面后仍会继续。'}</p>{analysis.error && <p className="chart-failure-reason">{refusalText(analysis.error) || `未完成原因（${analysis.error}）`}</p>}{analysis.status === 'WAITING_FOR_MODEL' && <button type="button" className="button primary" disabled={!provider?.ready || busy} onClick={() => { void start() }}>生成这份报告</button>}</div>}
        <ChartSynthesis analysis={analysis} />
        <ChartTradePlan analysis={analysis} />
        <ReportPlanReplay analysis={analysis} />
        {report && <ChartObservation key={analysis.id} analysis={analysis} />}
        {report && <ChartReview key={'review-'+analysis.id} analysis={analysis} />}
        {analysis.conclusion ? <details className="chart-strategy-details"><summary>全部策略证据与回放</summary><ChartAlgorithms analysis={analysis.algorithm_analysis} /></details> : <ChartAlgorithms analysis={analysis.algorithm_analysis} />}
        {report && <>
          <div className="chart-report-actions"><Badge tone={report.image_quality === 'READABLE' ? 'positive' : 'warning'}>{quality[report.image_quality]}</Badge><a className="button" href={chartAsset(analysis.id, 'report.md')} download><Download size={15} />下载报告</a><button className="button" onClick={() => window.print()}>打印 / 保存PDF</button></div>
          {!analysis.conclusion && <><section className="chart-report-summary"><span className="chart-kicker">分析摘要</span><h3>{trends[report.observed_trend]}的图中走势 · {trends[report.outlook]}的条件性展望</h3><p>{report.summary}</p><div className="chart-recognized">模型辨认：{report.recognized_symbol || '股票标识未能辨认'} · {report.recognized_period || '图中日期未能辨认'} · {report.recognized_timeframe ? timeframeNames[report.recognized_timeframe] : '周期未能辨认'}</div></section>
          <section className="chart-section"><h3>图中依据</h3><div className="chart-facts">{report.observations.map((row, index) => <article key={index}><Check size={16} /><div><p>{row.observation}</p><small>依据位置 · {row.image_region}</small></div></article>)}</div></section>
          <section className="chart-section"><h3>价位区域与成交量</h3><div className="chart-levels"><article><span>近似支撑区域</span><p>{report.support_zones.join('；') || '截图信息不足，无法确认。'}</p></article><article><span>近似阻力区域</span><p>{report.resistance_zones.join('；') || '截图信息不足，无法确认。'}</p></article></div><p className="chart-volume">{report.volume_comment}</p></section>
          <section className="chart-section"><h3>未来走势 · 三种条件性情景</h3><div className="chart-scenarios">{report.scenarios.map(row => <article key={row.direction} className={'chart-scenario ' + row.direction.toLowerCase()}><h4>{trends[row.direction]}</h4><span>触发条件</span><p>{row.trigger}</p><span>情景推演</span><p>{row.interpretation}</p><span>失效条件</span><p>{row.invalidation}</p></article>)}</div></section>
          <section className="chart-uncertainties"><h3>哪些信息还不能确认？</h3><ul>{report.uncertainties.map((value, i) => <li key={i}>{value}</li>)}</ul><p>单张截图无法证明未来涨跌。本报告是图像解读与条件性情景推演，未经预测准确性验证，不构成投资建议。</p></section>
          </>}
          {analysis.conclusion && <details className="chart-raw-report"><summary>原始识图叙述与图像依据（供核对）</summary><p>以下为模型原始叙述，单独归档，不作为有效综合结论。最终方向和参考动作见上方综合策略结论。</p><p>{report.summary}</p><p>模型辨认：{report.recognized_symbol || '代码未辨认'} · {report.recognized_period || '日期未辨认'}</p><ul>{report.observations.map((x,i)=><li key={i}>{x.observation}（{x.image_region}）</li>)}</ul><p>{report.volume_comment}</p><ul>{report.uncertainties.map((x,i)=><li key={i}>{x}</li>)}</ul></details>}
        </>}
        <ChartTimeline key={analysis.id} id={analysis.id} /><details className="chart-source"><summary>原图与报告来源</summary><img src={chartAsset(analysis.id, 'image')} alt="这份报告对应的原始股票走势图" /><p>用户确认的股票、日期与周期不等于已由外部数据独立核验。策略 {analysis.strategy_version} · {time(analysis.updated_at)}</p><code>SHA256 {analysis.image.sha256}</code></details>
      </> : <div className="chart-welcome"><FileImage size={46} /><span className="chart-kicker">一份报告，三层信息</span><h2>从截图到有依据的走势解读</h2><p>上传走势图，确认代码、K线周期与截止时间。报告将说明图中看到了什么、未来情景在什么条件下成立，以及还缺少哪些信息。</p><div><span>图中趋势与依据</span><span>支撑、阻力与成交量</span><span>偏强 / 震荡 / 偏弱情景</span></div><small>没有清晰信息时，系统应说明无法判断。</small></div>}
      <ChartForwardStatus /><section className="chart-history"><div className="chart-panel-title"><h2>分析记录</h2><span>{items.length} 份截图</span></div>{items.length ? items.map(item => <button type="button" key={item.id} data-analysis-id={item.id} aria-pressed={item.id === selected} className={item.id === selected ? 'selected' : ''} onClick={() => choose(item.id)}><div><strong>{item.request.symbol}</strong><span>{item.request.timeframe ? timeframeNames[item.request.timeframe] : '日线 · 旧版'} · {item.request.period_start} — {item.request.period_end}</span></div><span>{names[item.status]}</span><ArrowRight size={15} /></button>) : <p>上传第一张走势图后，记录会保留在这里。</p>}</section>
    </section></div>
    <footer className="chart-footer"><span>网页与QQ机器人共用分析接口</span><span>{provider?.qq_connected ? 'QQ指定私聊已连接' : 'QQ未连接或等待扫码'} · 交易权限关闭</span></footer>
  </div>
}
