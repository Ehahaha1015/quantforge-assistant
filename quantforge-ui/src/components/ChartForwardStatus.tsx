import { useEffect, useState } from 'react'
import { readForwardStatus, type ForwardStatus } from '../data/chartReports'

export function ChartForwardStatus(){
  const [value,setValue]=useState<ForwardStatus|null>(null)
  const [error,setError]=useState('')
  const refresh=async()=>{try{setValue(await readForwardStatus());setError('')}catch(e){setError((e as Error).message)}}
  useEffect(()=>{void refresh()},[])
  return <section className="chart-section" aria-label="前向记录"><h3>真实前向记录</h3>
    {value&&<><p>{value.status==='ACTIVE'?'已启用，按交易日收盘后逐步固化':value.status==='PAUSED_CODE_CHANGED'?'代码版本已变化，记录暂停，需新登记':value.status==='PAUSED_PROCESSING_FAILED'?'前向资料处理异常，记录已暂停，请核查':value.status==='PAUSED_CALENDAR_UNVERIFIED'?'该年度交易日历尚未核验，记录暂停':value.status==='PAUSED_STORAGE_LIMIT'?'前向资料存储达到上限，记录暂停':'尚未启用前向记录'}</p>
      <p>已固化 {value.frozen} 条 · 成熟 {value.matured} 条 · 等待 {value.pending} 条 · 漏记 {value.missed_days} 日</p><p>{value.note}</p></>}
    {error&&<p role="status">{error}</p>}<button className="button" type="button" onClick={()=>void refresh()}>刷新前向记录</button>
  </section>
}
