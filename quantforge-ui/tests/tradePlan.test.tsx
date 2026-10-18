import { afterEach, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { ChartTradePlan } from '../src/components/ChartTradePlan'
import { horizonText, parseTradePlan, type ChartAnalysis } from '../src/data/chartReports'

afterEach(cleanup)
const plan = { version: 'conditional-entry-exit-v1', action: 'CONSIDER_ENTRY', basis: '固定规则夹具，非效果证据', reasons: ['软件夹具条件通过'], indicators: { reference_close: '100.0000' }, levels: { reference: '100.0000', stop_reference: '96.0000', take_profit_reference: '106.0000', rule: '减2ATR/加3ATR示例' }, exit_rules: ['最多观察4根完整K线后复评'], max_observation_bars: 4, held_bars: 1, remaining_observation_bars: 3, position_basis: 'USER_COST', timeframe: '60m', execution_constraint: '普通A股T+1，买入当日不能卖出', as_of_bar: '2026-10-16T15:00:00+08:00' }
function snapshot(status: string) {
  return { request: { symbol: '600519', timeframe: '60m', period_start: '2026-10-01', period_end: '2026-10-16', period_end_time: '15:00' }, report: { image_quality: 'READABLE', recognized_symbol: '600519', recognized_timeframe: '60m', recognized_period: '2026-10-01 至 2026-10-16 截止15:00' }, algorithm_analysis: { trade_plan: parseTradePlan(plan) }, image_alignment: { status, note: status === 'CONSISTENT' ? '软件夹具识别一致' : '周期尚未核对' } } as ChartAnalysis
}

it('renders deterministic entry and exit references with units and settlement constraints', () => {
  render(<ChartTradePlan analysis={snapshot('CONSISTENT')} />)
  expect(screen.getByRole('heading',{name:'条件满足，可考虑购入'})).toBeInTheDocument()
  expect(screen.getByText('96.0000')).toBeInTheDocument()
  expect(screen.getByText(/剩余观察 3 根/)).toBeInTheDocument()
  expect(screen.getByText(/普通A股T\+1/)).toBeInTheDocument()
})

it.each(['MISMATCH','UNCONFIRMED'])('does not display buy/sell prices for an unverified screenshot: %s', status => {
  render(<ChartTradePlan analysis={snapshot(status)} />)
  expect(screen.getByRole('heading',{name:'先核对截图，暂不给购入或卖出结论'})).toBeInTheDocument()
  expect(screen.queryByText('96.0000')).not.toBeInTheDocument()
  expect(screen.queryByText('条件满足，可考虑购入')).not.toBeInTheDocument()
})

it('rejects invalid reference prices and false remaining holding durations', () => {
  expect(()=>parseTradePlan({...plan,levels:{...plan.levels,stop_reference:'NaN'}})).toThrow()
  expect(()=>parseTradePlan({...plan,remaining_observation_bars:99})).toThrow()
})

it.each(['2024-10-01 至 2024-10-16 截止15:00', '2026-10-01 至 2026-10-16 截止11:30', '日期不可辨认'])('rechecks dates and time even when an old stored gate passed: %s', period => {
  const value = snapshot('CONSISTENT')
  value.report!.recognized_period = period
  render(<ChartTradePlan analysis={value} />)
  expect(screen.queryByText('96.0000')).not.toBeInTheDocument()
  expect(screen.getByRole('heading', { name: '先核对截图，暂不给购入或卖出结论' })).toBeInTheDocument()
})

it('retains old daily horizons while labeling new hourly and weekly units', () => {
  expect(horizonText({horizon_sessions:5})).toBe('后续5根日线K线')
  expect(horizonText({horizon_sessions:5,timeframe:'60m',horizon_bars:4})).toBe('后续4根60分钟K线')
  expect(horizonText({horizon_sessions:5,timeframe:'1w',horizon_bars:1})).toBe('后续1根周线K线')
})
