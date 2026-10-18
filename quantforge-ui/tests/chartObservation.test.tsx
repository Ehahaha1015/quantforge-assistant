import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { ChartObservation } from '../src/components/ChartObservation'
import { parseAnalysis } from '../src/data/chartReports'
import { synthesisFixture } from './fixtures/synthesisFixture'

afterEach(()=>{cleanup();vi.unstubAllGlobals()})
it('checks only on request and distinguishes retrospective results from forward forecasts', async()=>{
  const fetch = vi.fn(async()=>({ok:true,json:async()=>({schema_version:'chart-observation-v1',status:'MATURED',horizon_bars:5,observed_bars:5,kind:'RETROSPECTIVE',actual:'BEARISH',correct:false,future_return:'-0.02',first_reference_touch:'BOTH_AMBIGUOUS_STOP_FIRST',note:'软件夹具，不是成交收益。',forecast_validated:false})}))
  vi.stubGlobal('fetch',fetch)
  render(<ChartObservation analysis={parseAnalysis(synthesisFixture)} />)
  expect(fetch).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button',{name:'检查观察期结果'}))
  expect(await screen.findByText('事后回看，不能计入真实前向预测成绩')).toBeInTheDocument()
  expect(screen.getByText(/与原倾向不一致/)).toBeInTheDocument()
  expect(screen.getByText(/同根K线同时触及/)).toBeInTheDocument()
  expect(fetch).toHaveBeenCalledTimes(1)
})
