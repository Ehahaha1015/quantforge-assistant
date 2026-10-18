import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { ChartReports } from '../src/pages/ChartReports'
import { ChartReview } from '../src/components/ChartReview'
import { ChartForwardStatus } from '../src/components/ChartForwardStatus'
import { ChartSynthesis } from '../src/components/ChartSynthesis'
import { parseAnalysis, reviewChart } from '../src/data/chartReports'
import { synthesisFixture } from './fixtures/synthesisFixture'

afterEach(()=>{cleanup();vi.unstubAllGlobals()})
function analysis(){const value=parseAnalysis(structuredClone(synthesisFixture));value.request.adjustment='RAW';value.report!.recognized_adjustment='RAW';return value}

it('keeps old conclusion readable and renders uncertainty on new conclusions',()=>{
  const value=analysis()
  value.conclusion={...value.conclusion!,schema_version:'chart-conclusion-v2',uncertainty_note:'证据不足不等于横盘。'}
  render(<ChartSynthesis analysis={value}/>);
  expect(screen.getByText('证据不足不等于横盘。')).toBeInTheDocument()
})

it('requests latest review only on click and preserves the original report',async()=>{
  const value=analysis(),original=structuredClone(value)
  const result={schema_version:'chart-review-v1',job_id:value.id,status:'CHANGED',original_direction:'RANGE',direction:'UNCLEAR',original_action:'WAIT',action:'WAIT',original_as_of:'2026-09-08T07:00:00+00:00',latest_as_of:'2026-09-15T07:00:00+00:00',held_bars:5,levels:null,reasons:['较长周期尚未确认。'],note:'复评另存，原报告不变。',forecast_validated:false,model_calls:0}
  const fetch=vi.fn(async()=>({ok:true,json:async()=>result}))
  vi.stubGlobal('fetch',fetch)
  render(<ChartReview analysis={value}/>);
  expect(fetch).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button',{name:'用最新行情复评'}))
  expect(await screen.findByText('原条件已有变化，请重新复核')).toBeInTheDocument()
  expect(fetch).toHaveBeenCalledTimes(1)
  expect(fetch.mock.calls[0]).toBeDefined()
  expect(value).toEqual(original)
})

it('refuses unknown adjustment and rejects fabricated levels on incomplete review',async()=>{
  const value=analysis();value.request.adjustment='UNKNOWN'
  render(<ChartReview analysis={value}/>);
  expect(screen.queryByRole('button',{name:'用最新行情复评'})).not.toBeInTheDocument()
  vi.stubGlobal('fetch',vi.fn(async()=>({ok:true,json:async()=>({schema_version:'chart-review-v1',job_id:value.id,status:'DATA_UNAVAILABLE',direction:'UNCLEAR',original_direction:'RANGE',original_as_of:'x',latest_as_of:null,reasons:[],note:'x',forecast_validated:false,model_calls:0,levels:{reference:'1'}})})))
  await expect(reviewChart(value.id)).rejects.toThrow('不能展示精确参考')
})

it('shows empty real-forward counts without inventing accuracy',async()=>{
  vi.stubGlobal('fetch',vi.fn(async()=>({ok:true,json:async()=>({schema_version:'daily-forward-status-v1',status:'ACTIVE',frozen:0,matured:0,pending:0,missed_days:0,failed_sources:0,model_calls:0,forecast_validated:false,note:'缺失不补造，不是发布验收。'})})))
  render(<ChartForwardStatus/>);
  expect(await screen.findByText(/已固化 0 条/)).toBeInTheDocument()
  expect(screen.queryByText(/准确率/)).not.toBeInTheDocument()
})

it('mounts the review action when a verified report is selected on the main page',async()=>{
  const value=analysis()
  const provider={schema_version:'chart-provider-status-v1',provider:'DeepSeek',model:'deepseek-flash',configured:false,ready:false,max_cost_cny:'0',reserved_cny:'0',calls_used:0,calls_limit:100,qq_connected:false,strategy_version:'fixture'}
  vi.stubGlobal('fetch',vi.fn(async(url:string)=>({ok:true,json:async()=>url.endsWith('/chart-settings')?provider:url.endsWith('/chart-forward')?{schema_version:'daily-forward-status-v1',status:'DISABLED',frozen:0,matured:0,pending:0,missed_days:0,failed_sources:0,model_calls:0,forecast_validated:false,note:'fixture'}:url.endsWith('/chart-analyses')?{schema_version:'chart-list-v1',items:[value]}:value})))
  render(<ChartReports/>);
  fireEvent.click(await screen.findByRole('button',{name:/600519.*报告已生成/}))
  expect(await screen.findByRole('button',{name:'用最新行情复评'})).toBeInTheDocument()
})
