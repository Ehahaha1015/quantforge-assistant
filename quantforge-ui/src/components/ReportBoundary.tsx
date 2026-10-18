import { Component, type ReactNode } from 'react'

export function ReportFailure({ message }: { message: string }) {
  return <main className="chart-product"><h1>报告页面暂时不可用</h1><p role="alert">{message}</p><p>请刷新页面，并确认本机报告服务正在运行。已保存的截图和报告仍保存在后端。</p></main>
}

export class ReportBoundary extends Component<{ children: ReactNode }, { message: string | null }> {
  state: { message: string | null } = { message: null }
  static getDerivedStateFromError(error: Error) { return { message: error.message } }
  render() { return this.state.message === null ? this.props.children : <ReportFailure message={this.state.message} /> }
}
