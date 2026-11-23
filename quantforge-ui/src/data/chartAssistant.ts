import { chartApi } from './chartApi'

export type Claim = { text: string; evidence_ids: string[] }
export type Evidence = { id: string; label: string; kind: 'COMPUTATION' | 'SOURCE' | 'METADATA' | 'MODEL_OBSERVATION'; path: string; value: unknown; note: string }
export type Brief = {
  schema_version: 'chart-assistant-brief-v1'; job_id: string; snapshot_sha256: string
  status: 'READY' | 'NEEDS_REVIEW' | 'DATA_UNAVAILABLE' | 'NOT_READY'; status_label: string
  as_of: string | null; offline: boolean; headline: string; focus_question: string
  supporting: Claim[]; opposing: Claim[]; next_checks: Claim[]; missing: Claim[]; evidence: Evidence[]
  scope_note: string; model_calls: 0; forecast_validated: false
}
export type Answer = {
  schema_version: 'chart-assistant-answer-v1'; job_id: string; snapshot_sha256: string; question: string
  intent: 'WHY' | 'RISK' | 'EXIT' | 'VOLUME' | 'DATA' | 'SCOPE' | 'OVERVIEW'
  status: 'ANSWERED' | 'INSUFFICIENT' | 'OUT_OF_SCOPE'; claims: Claim[]; evidence: Evidence[]
  note: string; model_calls: 0; forecast_validated: false
}
export type Comparison = {
  schema_version: 'chart-report-comparison-v1'; current_id: string; previous_id: string
  current_sha256: string; previous_sha256: string; changes: Claim[]; evidence: Evidence[]
  note: string; model_calls: 0; forecast_validated: false
}
const obj = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)
const sha = (v: unknown) => typeof v === 'string' && /^[a-f0-9]{64}$/.test(v)
function validateClaims(claims: unknown, evidence: unknown): boolean {
  if (!Array.isArray(evidence) || !evidence.every(e => obj(e) && typeof e.id === 'string' && typeof e.label === 'string' && ['COMPUTATION', 'SOURCE', 'METADATA', 'MODEL_OBSERVATION'].includes(String(e.kind)) && typeof e.path === 'string' && e.path.startsWith('/') && 'value' in e && typeof e.note === 'string')) return false
  const ids = new Set(evidence.map(e => e.id))
  return ids.size === evidence.length && Array.isArray(claims) && claims.every(c => obj(c) && typeof c.text === 'string' && Array.isArray(c.evidence_ids) && c.evidence_ids.length > 0 && c.evidence_ids.every(id => typeof id === 'string' && ids.has(id)))
}
export function parseBrief(v: unknown, id: string): Brief {
  if (!obj(v) || v.schema_version !== 'chart-assistant-brief-v1' || v.job_id !== id || !sha(v.snapshot_sha256) || v.forecast_validated !== false || v.model_calls !== 0 || !['READY', 'NEEDS_REVIEW', 'DATA_UNAVAILABLE', 'NOT_READY'].includes(String(v.status)) || typeof v.status_label !== 'string' || typeof v.headline !== 'string' || typeof v.focus_question !== 'string' || typeof v.scope_note !== 'string' || typeof v.offline !== 'boolean' || !(v.as_of === null || typeof v.as_of === 'string') || !['supporting', 'opposing', 'next_checks', 'missing'].every(k => validateClaims(v[k], v.evidence))) throw new Error('报告证据无法核验，请重新读取。')
  return v as Brief
}
export function parseAnswer(v: unknown, id: string, question: string): Answer {
  if (!obj(v) || v.schema_version !== 'chart-assistant-answer-v1' || v.job_id !== id || v.question !== question.trim() || !sha(v.snapshot_sha256) || v.forecast_validated !== false || v.model_calls !== 0 || typeof v.note !== 'string' || !['ANSWERED', 'INSUFFICIENT', 'OUT_OF_SCOPE'].includes(String(v.status)) || !['WHY', 'RISK', 'EXIT', 'VOLUME', 'DATA', 'SCOPE', 'OVERVIEW'].includes(String(v.intent)) || !validateClaims(v.claims, v.evidence)) throw new Error('回答与报告证据不一致，请重新读取。')
  return v as Answer
}
export function parseComparison(v: unknown, current: string, previous: string): Comparison {
  if (!obj(v) || v.schema_version !== 'chart-report-comparison-v1' || v.current_id !== current || v.previous_id !== previous || !sha(v.current_sha256) || !sha(v.previous_sha256) || v.forecast_validated !== false || v.model_calls !== 0 || typeof v.note !== 'string' || !validateClaims(v.changes, v.evidence)) throw new Error('报告比较来源无法核验。')
  return v as Comparison
}
async function request(path: string, signal?: AbortSignal, question?: string): Promise<unknown> {
  const boundedSignal = signal ? AbortSignal.any([signal, AbortSignal.timeout(20000)]) : AbortSignal.timeout(20000)
  const response = await fetch(`${chartApi}/api/chart-analyses/${path}`, question === undefined ? { signal: boundedSignal } : {
    signal: boundedSignal, method: 'POST', headers: { 'Content-Type': 'application/json', 'X-QF-Mode': 'RESEARCH' }, body: JSON.stringify({ question }),
  })
  if (!response.ok) throw new Error('报告解释暂不可用，请确认两份报告的证券、周期、复权和观察期限一致后重试。')
  return response.json()
}
export async function readBrief(id: string, signal?: AbortSignal) { return parseBrief(await request(`${encodeURIComponent(id)}/assistant`, signal), id) }
export async function askReport(id: string, question: string, signal?: AbortSignal) { return parseAnswer(await request(`${encodeURIComponent(id)}/question`, signal, question), id, question) }
export async function compareReports(current: string, previous: string, signal?: AbortSignal) { return parseComparison(await request(`${encodeURIComponent(current)}/comparison?previous_id=${encodeURIComponent(previous)}`, signal), current, previous) }
