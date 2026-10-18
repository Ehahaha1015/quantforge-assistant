import type { ReactNode } from 'react'
type Tone = 'positive' | 'warning' | 'negative' | 'neutral' | 'blue'
export function Badge({ children, tone = 'neutral' }: { children: ReactNode; tone?: Tone }) {
  return <span className={`badge ${tone}`}><span className="status-dot" />{children}</span>
}
export function PageHeading({ eyebrow, title, description, action }: { eyebrow: string; title: string; description: string; action?: ReactNode }) {
  return <div className="page-heading"><div><div className="eyebrow">{eyebrow}</div><h1>{title}</h1><p>{description}</p></div>{action}</div>
}
