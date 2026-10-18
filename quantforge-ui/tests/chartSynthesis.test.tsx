import { afterEach, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { ChartSynthesis } from '../src/components/ChartSynthesis'
import { ChartAlgorithms } from '../src/components/ChartAlgorithms'
import { parseAnalysis, parseTradePlan } from '../src/data/chartReports'
import { parseConclusion, parseSynthesis } from '../src/data/chartSynthesis'
import { synthesisFixture } from './fixtures/synthesisFixture'

afterEach(cleanup)

it('shows the combined backend conclusion with evidence and three conditional scenarios', () => {
  const analysis = parseAnalysis(synthesisFixture)
  render(<ChartSynthesis analysis={analysis} />)
  expect(screen.getByText(analysis.conclusion!.headline)).toBeInTheDocument()
  expect(screen.getByText('有效证据 8 / 8')).toBeInTheDocument()
  expect(screen.getByRole('heading',{name:'支持依据'})).toBeInTheDocument()
  expect(screen.getByRole('heading',{name:'反向证据与不足'})).toBeInTheDocument()
  expect(screen.getByRole('heading',{name:'趋势与动量'})).toBeInTheDocument()
  expect(screen.getByRole('heading',{name:'突破与量能'})).toBeInTheDocument()
  expect(screen.getByRole('heading',{name:'回归与超跌'})).toBeInTheDocument()
  expect(screen.getByRole('heading',{name:'未来走势 · 三种条件性情景'})).toBeInTheDocument()
  expect(screen.queryByText(analysis.report!.summary)).not.toBeInTheDocument()
  expect(screen.getByText(/预测效果未验证/)).toBeInTheDocument()
})

it('exposes all eight direction explanations separately from research positions',()=>{
  render(<ChartAlgorithms analysis={parseAnalysis(synthesisFixture).algorithm_analysis} />)
  expect(screen.getAllByText(/方向证据：/)).toHaveLength(8)
  expect(screen.getByText('RSI越过超跌/超买后的变化需价格确认；仅超卖不判断上涨。')).toBeInTheDocument()
})

it('rechecks image dates before displaying even a stored ready conclusion', () => {
  const changed = JSON.parse(JSON.stringify(synthesisFixture))
  changed.report.recognized_period = '2024-01-01 至 2024-01-31'
  render(<ChartSynthesis analysis={parseAnalysis(changed)} />)
  expect(screen.getByText('先核对截图，当前无法形成有效综合结论。')).toBeInTheDocument()
  expect(screen.queryByText(changed.conclusion.headline)).not.toBeInTheDocument()
  expect(screen.queryByRole('heading',{name:'未来走势 · 三种条件性情景'})).not.toBeInTheDocument()
})

it('rejects invented probability authority, duplicated evidence and missing synthesis', () => {
  const analysis = parseAnalysis(synthesisFixture)
  expect(()=>parseConclusion({...analysis.conclusion,forecast_validated:true})).toThrow()
  const synthesis = analysis.algorithm_analysis!.synthesis!
  expect(()=>parseSynthesis({...synthesis,evidence:Array(8).fill(synthesis.evidence[0])})).toThrow()
  expect(()=>parseAnalysis({...analysis,algorithm_analysis:{...analysis.algorithm_analysis,synthesis:null}})).toThrow()
  expect(()=>parseTradePlan({...analysis.algorithm_analysis!.trade_plan,version:'unknown'})).toThrow()
  expect(()=>parseAnalysis({...analysis,conclusion:{...analysis.conclusion,direction:'BEARISH'}})).toThrow('综合结论与算法不一致')
})
