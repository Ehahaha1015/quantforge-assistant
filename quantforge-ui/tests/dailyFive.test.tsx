import { afterEach, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { ReportPlanReplay } from '../src/components/ReportPlanReplay'
import { parseAnalysis, parseReportPlanReplay, parseTradePlan } from '../src/data/chartReports'
import { effectiveAlignment } from '../src/data/chartAlignment'
import { synthesisFixture } from './fixtures/synthesisFixture'

afterEach(cleanup)
const replay = {schema_version:'report-plan-replay-v1',policy:'BASELINE_V2',forecast_horizon_bars:5,max_holding_bars:10,review_interval_bars:1,scope:'RESEARCH_ONLY',limitations:['明确的软件回放夹具'],summaries:[10,30].map(friction=>({friction_bps_each_side:friction,price_return_after_assumed_costs:'0',max_drawdown:'0',closed_trades:0,open_quantity:0,blocked_attempts:1,entries:0,execution_qualified:false,artifact_sha256:'a'.repeat(64)}))}

it('keeps five-bar forecast independent from ten-bar holding and hides unverified prices',()=>{
  const analysis=parseAnalysis(synthesisFixture)
  const plan=parseTradePlan({...analysis.algorithm_analysis!.trade_plan,version:'conditional-entry-exit-v3',forecast_horizon_bars:5,review_interval_bars:1,max_observation_bars:10,remaining_observation_bars:10})
  expect(plan.forecast_horizon_bars).toBe(5)
  expect(plan.max_observation_bars).toBe(10)
  expect(()=>parseTradePlan({...plan,forecast_horizon_bars:10})).toThrow()
  analysis.request.adjustment='UNKNOWN'
  expect(effectiveAlignment(analysis).status).toBe('UNCONFIRMED')
  analysis.algorithm_analysis!.report_plan_replay=parseReportPlanReplay(replay)
  render(<ReportPlanReplay analysis={analysis} />)
  expect(screen.getByText(/暂不展示这份报告的回放数值/)).toBeInTheDocument()
  expect(screen.queryByRole('table')).not.toBeInTheDocument()
})

it('shows shared-rule cost comparisons without calling zero trades a successful forecast',()=>{
  const analysis=parseAnalysis(synthesisFixture)
  analysis.request.adjustment='RAW'
  analysis.report!.recognized_adjustment='RAW'
  analysis.algorithm_analysis!.report_plan_replay=parseReportPlanReplay(replay)
  render(<ReportPlanReplay analysis={analysis} />)
  expect(screen.getByText('10bp')).toBeInTheDocument()
  expect(screen.getByText('30bp')).toBeInTheDocument()
  expect(screen.getByText(/零回放变化不表示预测成功/)).toBeInTheDocument()
  analysis.report!.recognized_adjustment='QFQ'
  expect(effectiveAlignment(analysis).status).toBe('MISMATCH')
  expect(()=>parseReportPlanReplay({...replay,summaries:[{...replay.summaries[0],execution_qualified:true}]})).toThrow()
  expect(()=>parseReportPlanReplay({...replay,max_holding_bars:99})).toThrow()
})
