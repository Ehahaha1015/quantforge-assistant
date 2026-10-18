import { useEffect, useState } from 'react'
import { chartApi } from '../data/chartApi'

type Timeline = { schema_version: 'chart-events-v1'; job_id: string; items: { seq: number; at: string; kind: string; status: string | null; error: string | null }[]; note: string }
const labels: Record<string, string> = { SCREENSHOT_RECEIVED: '截图已接收', CORE_ALGORITHMS_RECORDED: '规则计算已保存', ANALYSIS_STARTED: '调用已预留并开始', ANALYSIS_SUCCEEDED: '报告已保存', ANALYSIS_FAILED: '处理失败，原因已保留', ANALYSIS_UNKNOWN: '外部调用结果未知', INTERRUPTED_CALL_NOT_REPLAYED: '中断已登记，未重发调用' }

export function ChartDemoNotice() {
  const [demo, setDemo] = useState(false)
  useEffect(() => { let active = true; void fetch(chartApi + '/api/health').then(r => r.json()).then(v => { if (active) setDemo(v.demo === true) }).catch(() => {}); return () => { active = false } }, [])
  return demo ? <aside className="chart-demo-notice" aria-label="离线演示说明"><strong>离线工程演示 · 未调用真实模型</strong><p>四份合成样例展示：成功、资料不足、非法输出和结果未知。行情与识图输出均为软件夹具，不代表真实市场或模型准确率。</p><a href={chartApi + '/api/docs'} target="_blank" rel="noreferrer">查看 API 契约</a></aside> : null
}

export function ChartTimeline({ id }: { id: string }) {
  const [value, setValue] = useState<Timeline | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const load = async () => {
    if (loading) return
    setLoading(true)
    try {
      const response = await fetch(chartApi + '/api/chart-analyses/' + encodeURIComponent(id) + '/events')
      const result = await response.json()
      if (!response.ok || result.schema_version !== 'chart-events-v1' || result.job_id !== id || !Array.isArray(result.items)) throw new Error('任务时间线暂不可用')
      setValue(result); setError('')
    } catch { setError('任务时间线暂不可用') } finally { setLoading(false) }
  }
  return <details className="chart-source" onToggle={e => { if (e.currentTarget.open && !value) void load() }}><summary>处理过程与失败原因</summary>
    <p>按需读取脱敏状态事件，可用于核对任务是否完成；不展示密钥、原始请求或个人对话。</p>
    {error && <p role="status">{error}</p>}
    {value && <><ol>{value.items.map(row => <li key={row.seq}><strong>{labels[row.kind] || row.kind}</strong> · {row.status || '已登记'}<br /><time>{new Date(row.at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })}</time>{row.error && <p>{row.error}</p>}</li>)}</ol><p>{value.note}</p></>}
  </details>
}
