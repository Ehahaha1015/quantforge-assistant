import { ChartNoAxesCombined } from 'lucide-react'
import { ChartReports } from './pages/ChartReports'

export default function ChartApp() {
  return <div className="chart-product">
    <a className="skip-link" href="#main-content">跳转到主内容</a>
    <header className="chart-product-header"><a href="#chart-reports" aria-label="QuantForge 股票截图报告"><ChartNoAxesCombined size={23} /><strong>QuantForge</strong><span>股票分析与报告助手</span></a><span>本机工作区</span></header>
    <main id="main-content" tabIndex={-1}><ChartReports /></main>
  </div>
}
