# QuantForge

Fixed buffered-trend ablations add causal entry/exit bands, optional long-trend
filters and exposure diagnostics. The multi-ETF historical study retains every
variant and cost/latency stress case; see `../docs/TREND-REFINEMENT-20261013.md`.

The bounded strategy-optimization research module adds chronological, cost-aware
parameter selection for SMA and time-series momentum, cash fallback and matched
baselines. Historical CSV runs use the Platform CLI and remain exploratory.
See `../docs/STRATEGY-OPTIMIZATION-20261013.md`; no live authority is granted.

Deterministic quantitative research and historical paper execution, with an independent AI research laboratory. AI chooses hypotheses and research directions. Core owns admission, data checks, calculations, evidence, risk and fills. Real broker transport remains locked.

Research Room can reuse an integrity-verified pilot snapshot through
`ResearchService.import_pilot_dataset`, declaring the interval and prior
exposure. It binds the original source claims, limitations and selected content
hash into a verified host qualification. This is an operator-declared historical
source, not independent exchange authentication. Existing cross-program exposure
checks and sealed final holdouts remain in effect. New Lab work uses `loop` or
the Platform protected-profile research CLI; paid legacy examples require
explicit `--legacy` compatibility mode.

V1 supports a fixed long/flat SMA crossover, explicit 10 bps position-switch fees, development/validation rounds, one post-freeze final holdout, exact development reproduction and a persistent paper stream. A valid experiment proves computation, not alpha.

For a one-lot 100 CNY manual execution experiment, see [MANUAL_CANARY.md](docs/MANUAL_CANARY.md).
`quantforge canary-check` checks current operator observations, exact fees, exit reserve,
the total budget and existing Risk Engine. It emits a manual review plan only; broker transport stays locked.

## Install

Use Python 3.12 and uv. Install Core and the sibling qf-lab independently:

```powershell
cd D:\QuantForge\quantforge
uv sync --locked --group dev
cd D:\QuantForge\qf-lab
uv sync --locked --group dev
```

Lab's pyproject.toml binds its local Core source checkout. For a different checkout, change that path and regenerate the Lab lockfile. Both projects must use the original bound Core/runtime versions for exact reproduction. Source bytes, Git SHA, lockfile, runtime, dataset and executable parameters are bound in each experiment.

## Research demo

In the Lab directory, create a fresh local artifact root once. Keep generated evidence outside source control.

```powershell
$qfRoot = 'D:\QuantForge\local-smoke\qfv1-demo'
$qfLab = 'D:\QuantForge\local-smoke\qfv1-intelligence'
New-Item -ItemType Directory -Force -Path $qfRoot
uv run qf-lab --core-root $qfRoot --lab-root $qfLab data
# Use the returned dataset_id below.
uv run qf-lab --core-root $qfRoot --lab-root $qfLab research 'Test a transparent cost-aware IBM SMA hypothesis with a final holdout' --dataset <dataset_id> --program ibm-study
uv run qf-lab --core-root $qfRoot --lab-root $qfLab inspect ibm-study
uv run qf-lab --core-root $qfRoot --lab-root $qfLab programs
```

Configure DEEPSEEK_API_KEY securely, or use the existing DSH credential reference. Never paste secrets into a goal, log or checkpoint. The one real provider is the documented deepseek-flash Chat Completions endpoint. Each program has three model calls, bounded context/output, schema/action allowlists and durable request/response/error records. Ambiguous model calls are never silently retried. `--provider fake` is explicitly labelled deterministic test mode.

Round one passes Core admission. The model reads development evidence, changes its parameter pair for round two, then compares both immutable evidence IDs to freeze/reject an already-run candidate. Each development run independently reproduces. Interrupted program IDs cannot be silently restarted; inspect their histories.

Use `programs` or `inspect <program>` from another process while research runs to see persisted progress, proposals and evidence references. IN_PROGRESS_OR_INTERRUPTED indicates unfinished recorded history; it does not certify that the originating process is alive. Queries never create missing history. Expected precondition failures return BLOCKED JSON and exit code 2.

In the Core directory:

```powershell
uv run quantforge --root $qfRoot runs
uv run quantforge --root $qfRoot inspect ibm-study-r1
uv run quantforge --root $qfRoot reproduce ibm-study-r1
uv run quantforge --root $qfRoot dataset <dataset_id>
# Requires a critic-frozen, reproduced candidate:
uv run quantforge --root $qfRoot final ibm-study
uv run quantforge --root $qfRoot final-inspect ibm-study
```

EvidencePack.json is canonical; Markdown is derived. Assertions belong to FACT, DECLARED_ASSUMPTION or LIMITATION. DEVELOPMENT calculations receive only development/validation prefixes. The existing immutable OOS seal reserves the final holdout after candidate freeze. Identical machine recomputation is proven before terminal closure; later final inspection verifies the recorded proof without reopening the terminal holdout. Failed reservations remain spent history.

Families are bounded to eight retained reservations, including failures. Relabelling the same snapshot under another program/family is rejected. Freeze records selection, exposures and an explicitly limited Bonferroni normal mean-return diagnostic. It grants no validated-alpha status. Broad search/live promotion are unavailable.

## Paper demo

A frozen reproduced candidate must pass the final loss screen. Paper uses the previous observation's signal at the next open, frozen risk checks, whole-unit 10% cash allocation, fees and Decimal accounting.

```powershell
uv run quantforge --root $qfRoot paper-run ibm-study --steps 40
uv run quantforge --root $qfRoot paper-status ibm-study
# A fresh process continues at its persisted cursor:
uv run quantforge --root $qfRoot paper-run ibm-study --steps 20
uv run quantforge --root $qfRoot paper-history ibm-study
uv run quantforge --root $qfRoot paper-stop ibm-study
```

`paper-run --watch` remains active until paper-stop from another process. V1 consumes immutable historical data; at exhaustion it reports WAITING_FOR_DATA. There is no live paper feed. Intent, risk, fill, accounting state and cursor commit together in append-only SQLite history. Restart does not redispatch committed fills. Host-owned local storage and one serialized writer are required; Windows directory/power-loss durability is not claimed.

## CN small-capital pilot (paper only)

An additive profile that models the frictions a real 100 CNY A-share account faces (board lots, minimum commission, stamp duty, T+1, price limits, recovered CNY price scale) and records one order ticket per session for later manual execution.

```powershell
uv run quantforge pilot --root <pilot-root> import-corpus --provider-root <cn_data> --universe <universe.json> --symbol SH600008 --as-of <aware-UTC>
uv run quantforge pilot --root <pilot-root> declare --program <id> --purpose "<honest sentence>" --snapshot <snapshot-id> --symbol SH600008 --short 12 --long 42 --capital 100.00
uv run quantforge pilot --root <pilot-root> run --steps 0
uv run quantforge pilot --root <pilot-root> tickets | reconcile | status
```

See [docs/CN_PILOT.md](docs/CN_PILOT.md) for the declared rule profiles, the two real-data defects found and fixed, the retained demonstration runs and the remaining gaps. `real_transport` stays `LOCKED`; no broker, credentials or live feed exist in this profile.

## Quality and limitations

```powershell
uv run pytest -q
uv run ruff check .
uv run mypy src
git diff --check
```

CI runs canonical gates on Linux and Windows. Lab has independent tests/lint/types. [Authority decision](docs/architecture/ADR-V1-001-research-authority.md) and [developer notes](docs/PRODUCT_DEVELOPMENT.md) explain boundaries.

The official public IBM demo provides 100 raw daily bars. UTC date labels represent US sessions, not observed intraday/PIT timestamps. Calendar gaps are recorded, not independently certified. No survivorship-free universe, delisted coverage, corporate-action perfection, redistribution entitlement or profitability claim is made. Slippage, dividends, financing and market impact are unmodelled. Artifacts remain local. Broader data requires appropriate credentials/entitlements. Optional Qlib is outside this v1 slice.

Core imports no model SDK or Lab package. Lab imports no raw fact store or live authority. Existing real broker adapters stay hard locked. No real orders are possible through the product entrypoint.

## Development

The original architecture principles remain enforced: no model-authoritative facts, no model SDK in Core, fail-closed admission/credentials, Decimal monetary accounting, append-only execution state and conservative crash recovery. Boundary tests under tests/boundaries complement the product authority tests.

See [development guide](docs/DEVELOPMENT.md) for canonical checks and optional hooks. Use an isolated branch/worktree, declare write scope, preserve human changes and frozen contracts, and commit only reviewed passing changes. Local integration is sufficient; publishing requires separate authorization. License and commercial redistribution rights have not been established.
