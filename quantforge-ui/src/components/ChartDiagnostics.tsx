import { useEffect, useRef, useState } from 'react'
import { chartApi } from '../data/chartApi'
import { refusalText } from '../data/chartReports'

type Timeline = { schema_version: 'chart-events-v1'; job_id: string; items: { seq: number; at: string; kind: string; status: string | null; error: string | null }[]; note: string }
const labels: Record<string, string> = { SCREENSHOT_RECEIVED: '截图已接收', CORE_ALGORITHMS_RECORDED: '规则计算已保存', MODEL_CALL_RESERVED: '调用预算已预留', ANALYSIS_STARTED: '调用已预留并开始', ANALYSIS_SUCCEEDED: '报告已保存', ANALYSIS_FAILED: '处理失败，原因已保留', ANALYSIS_UNKNOWN: '外部调用结果未知', INTERRUPTED_CALL_NOT_REPLAYED: '中断已登记，未重发调用' }
const statuses: Record<string, string> = { WAITING_FOR_MODEL: '等待识图服务', QUEUED: '排队中', RUNNING: '正在分析', SUCCEEDED: '报告已生成', FAILED: '生成未完成', UNKNOWN: '调用结果待核查' }

export function ChartDemoNotice({ onModeChange }: { onModeChange?: (demo: boolean | null) => void }) {
  const [demo, setDemo] = useState<boolean | null>(null)
  const [failed, setFailed] = useState(false)
  const [attempt, setAttempt] = useState(0)
  useEffect(() => {
    let active = true, timer: number
    const controller = new AbortController()
    const refresh = async () => {
      try {
        const response = await fetch(chartApi + '/api/health', { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(5000)]) })
        const health = await response.json()
        if (!response.ok || health.schema_version !== 'qf-health-v1' || typeof health.demo !== 'boolean') throw new Error('工作区模式无法核验')
        if (active) { setDemo(health.demo); setFailed(false); onModeChange?.(health.demo) }
      } catch {
        if (active) { setDemo(null); setFailed(true); onModeChange?.(null) }
      } finally { if (active) timer = window.setTimeout(refresh, 5000) }
    }
    void refresh()
    return () => { active = false; controller.abort(); window.clearTimeout(timer) }
  }, [attempt, onModeChange])
  if (demo === false) return null
  return <aside className="chart-demo-notice" aria-label={demo ? '离线演示说明' : '工作区连接状态'}>
    {demo ? <><strong>离线工程演示 · 未调用真实模型</strong><p>四份合成样例展示：成功、资料不足、非法输出和结果未知。行情与识图输出均为软件夹具，不代表真实市场或模型准确率。演示中仅浏览样例，上传和模型配置已关闭。</p><a href={chartApi + '/api/docs'} target="_blank" rel="noreferrer">查看 API 契约</a></> : <><strong>{failed ? '暂时无法确认工作区模式' : '正在确认工作区模式…'}</strong><p>连接确认前暂停上传与配置。请检查演示服务是否启动。</p>{failed && <button className="button" type="button" onClick={() => setAttempt(value => value + 1)}>重新连接工作区</button>}</>}
  </aside>
}

export function ChartTimeline({ id }: { id: string }) {
  const [value, setValue] = useState<Timeline | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const request = useRef<AbortController | null>(null)
  useEffect(() => {
    setValue(null); setError(''); setLoading(false)
    return () => request.current?.abort()
  }, [id])
  const load = async () => {
    if (request.current && !request.current.signal.aborted && loading) return
    const controller = new AbortController()
    request.current = controller
    setLoading(true)
    try {
      const response = await fetch(chartApi + '/api/chart-analyses/' + encodeURIComponent(id) + '/events', { signal: AbortSignal.any([controller.signal, AbortSignal.timeout(20000)]) })
      const result = await response.json()
      if (!response.ok || result.schema_version !== 'chart-events-v1' || result.job_id !== id || !Array.isArray(result.items)) throw new Error('任务时间线暂不可用')
      if (!controller.signal.aborted) { setValue(result); setError('') }
    } catch { if (!controller.signal.aborted) setError('任务时间线暂不可用') } finally { if (!controller.signal.aborted) setLoading(false) }
  }
  return <details className="chart-source" onToggle={e => { if (e.currentTarget.open && !value) void load() }}><summary>处理过程与失败原因</summary>
    <p>按需读取脱敏状态事件，可用于核对任务是否完成；不展示密钥、原始请求或个人对话。</p>
    {error && <p role="status">{error}</p>}
    <button className="button" type="button" disabled={loading} onClick={() => { void load() }}>{loading ? '正在读取处理过程…' : value ? '刷新处理过程' : '重新读取处理过程'}</button>
    {value && <><ol>{value.items.map(row => <li key={row.seq}><strong>{labels[row.kind] || row.kind}</strong> · {row.status ? statuses[row.status] || row.status : '已登记'}<br /><time>{new Date(row.at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })}</time>{row.error && <p>{refusalText(row.error) || row.error}</p>}</li>)}</ol><p>{value.note}</p></>}
  </details>
}
