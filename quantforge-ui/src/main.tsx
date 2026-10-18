import React from 'react'
import ReactDOM from 'react-dom/client'
import './styles.css'
import { ReportBoundary, ReportFailure } from './components/ReportBoundary'
const root = ReactDOM.createRoot(document.getElementById('root')!)
import('./ChartApp').then(({ default: App }) => {
  root.render(<React.StrictMode><ReportBoundary><App /></ReportBoundary></React.StrictMode>)
}).catch(error => {
  root.render(<ReportFailure message={error instanceof Error ? error.message : '报告页面未能加载。'} />)
})
