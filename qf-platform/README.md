# QuantForge local platform

For the fixed multi-ETF trend study, use `python -m qf_platform.refinement_cli`.
It freezes ten baseline/slow/buffered/filtered variants, records missing dates
and writes all cost/latency scenarios. See `../docs/TREND-REFINEMENT-20261013.md`.

## Historical strategy research

`python -m qf_platform.optimization_cli` evaluates a declared historical CSV
with frozen SMA/momentum candidates, past-only selection, cost stress and cash
fallback. It writes source-bound JSON, Markdown and CSV under an isolated run.
See `../docs/STRATEGY-OPTIMIZATION-20261013.md` for the actual 510300 study,
reproduction command and limitations. The offline task demo is unchanged.

## Offline assistant task workspace

From the workspace root, run `python governance-tools/assistant_demo.py start`.
Open http://127.0.0.1:4180/#tasks. The zero-key scripted demo creates durable
tasks, calls bounded Core experiment tools, compares fees, retains evidence and
exports reports. `run`, `status` and `stop` use the same launcher.
See `../docs/ASSISTANT-FIRST-VERSION-20261013.md` and TASK-WORKFLOW-ADR.md.
Synthetic task receipts do not establish alpha or live authority.

Payment collection and top-ups were removed on 2026-10-12. No merchant routes,
provider profiles, wallets or payment adapters are available. Existing immutable
simulation journals remain readable; legacy wallet funds are excluded from the
current trading equity and funding projection.


## Protected-profile research CLI

```powershell
python -m qf_platform.research_cli --root <existing-platform-root> run --dataset <id> --goal "<question>" --rounds 1 --budget-calls 6 --budget-tokens 250000 --budget-cost 0.25 --budget-wall 600
```

This uses the UI's protected binding registry, role routing and cumulative
profile budget. It never creates a credential registry or resets usage for a
new campaign. `status`, `memory` and `report` are read/derived operations;
`--program` selects a retained campaign. The installed `qf-research` alias is
available after syncing when no running Windows entrypoint holds the environment
open. Module invocation works in an already-running development environment.

Historical pilot data can be admitted through
`ResearchService.import_pilot_dataset` with an explicit date interval and prior
exposure declaration. Operator data retains its original limitations and does
not become exchange-authenticated or unseen-alpha evidence.

Local application using the existing deterministic Core and Lab proposal facade.
This is an operable simulation vertical slice, not a qualified real-money platform.

```powershell
cd D:\QuantForge\qf-platform
uv sync
uv run qf-platform --root D:\QuantForge\local-smoke\platform-simulation-20261011 --port 4188
# Start the existing front end separately; route #platform.
```

The HTTP API binds only to 127.0.0.1. Allowed browser origins are 4173, the
isolated 4179 console and local test ports 4180..4182. No external browser origins are allowed. Protected external bindings use the
separate domains described below.
Simulation POST routes require `Content-Type: application/json`, `X-QF-Mode: SIMULATION`
and a bounded `Idempotency-Key`. Replay returns current state; changed payload
with the same key is refused. JSON money is an exact decimal string.

| Route | Behavior |
| --- | --- |
| GET `/api/health` | Explicit simulation / locked real transport |
| GET `/api/snapshot` | Hash-verified local funds, positions, fills and event window |
| GET `/api/events` | SSE state each second; bounded to 8 concurrent streams |
| POST `/api/transfers` | `from`, `to`, `amount`; bank ↔ sim-a/sim-b only |
| POST `/api/research/cycle` | Empty object; 3 Lab FakeProvider calls + Core signals/risk/fills |
| POST `/api/control` | `paused` bool; opening pause, exits remain possible |
| POST `/api/autonomy` | Persist `enabled`, bounded `rounds`, `interval_seconds` (6..60); background scheduler |

Simulation starts with declared synthetic bank funds 10,000 CNY. The controller
allocates up to 2,500 CNY/account, respects account and global gross limits,
uses a declared ETF profile with 5 CNY minimum commission and board lots,
and runs rising/falling synthetic sessions to test entry and exit. It does
not represent current prices, exchange calendars, observed broker fills or alpha.

The research bridge records three bounded structured decisions using the
existing Lab ModelGateway; Core computes causal SMA signals on an 80-bar fixture.
The supplied model is explicitly fake and has no execution tool. This mechanism
does not implement LAB-101..104, OOS qualification, MTC, a profitable research
system, or the frozen live-execution composition. Use the separate existing
research machine for real experiments; those retained facts are not modified.

All simulated effects and the request receipt commit in one SQLite transaction.
The journal is append-only and hash verified; tampering blocks reads. Research
calls have a separate immutable Lab log. A crash after research starts but before
the funds transaction commits requires inspection and never silently reruns that
request. There is no external side effect to retry. Money conservation checks
every effect. The snapshot windows retain 100 events/fills;
complete records remain in `platform.sqlite`. A root permits 20 research cycles
; create a new declared root for a new qualification.

Autonomous simulation is a bounded background service. The browser starts a
4-round batch at 6-second intervals; the server records its budget, run identity
and next due time. It continues after the browser closes. Restart resumes the
recorded batch; incomplete research logs block continuation without blind retry.
Stopping is rechecked inside the serialized transaction before a scheduled cycle.
The UI disables new operations when its stream is stale, but does not claim that
a disconnected browser has stopped the service. The opening pause is persisted.
Historical risk/fill provenance remains in event
details; journal hashes provide integrity, not external provider authenticity.

To qualify: `uv run pytest -q`, `uv run ruff check .`, `uv run mypy src`.
Real broker bindings require separate official adapters, permissions,
authentication, credential isolation, reconciliation and qualified durable execution.
Do not promote this fixture to real mode by changing a label or flag.

Known bounds: the existing Risk-001 notional cap also applies to sells, so
appreciated holdings above that cap may require a separately qualified exit
design (no cap bypass here). A transient SQLite error parks the scheduler for
operator review rather than retrying a possibly started research attempt.

Committed browser qualification lives in the UI's `scripts/check_platform_browser.mjs`
and `scripts/platform_browser_flow.js`: start API and UI first, then
`npm run test:platform`. It writes the two tracked platform screenshots and
local JSON including axe/overflow, retired payment UI absence, research fills, background
continuation with the platform route unmounted, opening pause and stop results.
Use a stopped, unpaused simulation root with at least four of twenty cycles left.

## Real-binding software (financial experiments paused)

Use a declared local root and port 4192 for the current integrated console.
`#bindings`, `#research-room` and `#operations` in the frontend use this API.
Simulation on 4188 is retained independently. Financial authority is PAUSED and
operator state is disabled by default and persists after kill/restart. There is
no production enable or submit endpoint; model decisions cannot grant authority.

| API domain | Required POST mode | Responsibility |
| --- | --- | --- |
| `/api/bindings/*` | CONFIGURATION | Protected save/rotation/deletion, model test/discovery, role assignment |
| `/api/room/*` | RESEARCH | Bounded campaign, history selection, pause/stop/resume and retained review |
| `/api/brokers/*` | OPERATIONS | Official plugin authenticated observations/quotes/query, persistent kill |
| `/api/canary/plan` | OPERATIONS | Strict Decimal arithmetic; no authority or financial side effect |

GET `/api/bindings` includes masked profiles, frozen roles and conservative
profile-lifetime request/token/cost reservations. Connection tests are auditable
paid requests. Cross-provider proof requires a second configured real profile.
GET `/api/room/evidence?run_id=...` exposes only public Core context belonging to
the selected campaign, never hidden final/lockbox data. Review-only mode adds no
trials, cannot freeze/promote and retains the original source-version identity.

`BrokerService.execution_session` composes official SDK plugins with independent
CoreAccountingBook opening-cash/fill replay, fresh risk/provenance, Frontier, operator evidence,
atomic EXECSTATE claims, purpose-scoped Windows credential leases, observation
ledger and reconciliation. The plugin must provide its official docs/version and
capabilities; no vendor is selected or installed without a configured account.
Timeout after the durable send claim stays UNKNOWN across restart and cannot
blindly resend. Production remains denied; authenticated read-only queries remain
available after kill to reconcile uncertainty. See PROTOCOL-SOURCES.md.

GET `/api/history?domain=broker|simulation&after=0&limit=50` provides complete
paginated domain-isolated history. No credentials, raw callbacks, payer identities
or private keys enter these product rows. Current external dependencies: a selected
official broker/account/API permission, and a second
model provider for cross-provider qualification. No alpha is established.
