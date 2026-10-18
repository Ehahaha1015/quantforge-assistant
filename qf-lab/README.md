# QuantForge Lab

Independent intelligence for QuantForge. Models propose restricted hypotheses and choose research directions; Core alone computes, admits, records and promotes. Lab imports no raw Core fact-store or broker/execution authority.

## Current research entrypoints

New research uses `qf-lab loop` (explicit environment-reference routing) or
`python -m qf_platform.research_cli` from the Platform environment (the same
protected profiles and lifetime budgets as the UI). Legacy paid `research`
and `scale run/resume` require an explicit `--legacy`; they remain compatibility
paths for retained work, not the recommended new research workflow. Fake
legacy fixtures and history inspection remain available.

```powershell
uv run qf-lab --core-root <core-root> --lab-root <rooms-root> loop run --program <id> --goal "<research question>" --dataset <dataset-id> --provider configured --routing <role-routing.json> --rounds 1 --budget-calls 6 --budget-tokens 250000 --budget-cost 0.25 --budget-wall 600
uv run qf-lab --core-root <core-root> --lab-root <rooms-root> loop report --program <id>
```

The Room freezes a bounded cross-room memory snapshot at declaration. It
rebuilds completed source rooms read-only, preserves dataset/exposure identities
and separates technical failures, insufficient evidence and research rejection.
It transfers historical opinions and Core evidence references, never final
holdout values or fresh experiment permissions. `qf_lab.library.library` offers
the complete derived hypotheses, critique, failure and frozen-strategy views.
Regime evidence is not implemented. Shared endpoint/model roles explicitly
report degraded diversity; distinct routes do not certify independence.

The protocol transport offers short evidence aliases and strictly expands only
provided references. Unknown IDs, omitted evidence, invalid family parameters
and extra role fields remain errors. A terminal rejection after experiments is
a valid research outcome; technical failure before evidence is counted separately.
Reports include PBO/DSR applicability and exact Core version/runtime reproduction.

`qf_lab.comparison.Comparison` is a bounded component acceptance harness for a
frozen six-candidate universe. Fixed rules, seeded random selection, Researcher,
Researcher+Critic and Researcher+Critic+memory each inspect at most two chosen
development EvidencePacks per campaign. It reproduces and reuses Core facts;
it cannot add experiments, freeze candidates or open a final partition. Memory
contains a frozen historical view plus only prior completed campaigns in that
arm. Paid calls use the existing RoomGateway and protected profile lifetime
accounting. Its results concern finite candidate selection and operational
quality, not open-ended generation or independent market/alpha inference.

Observed comparison runs motivated explicit dataset identity labels in memory,
current remaining-view counts and a host check that revisions are distinct.
`unseen_candidates` can prefilter a future declared search using retained strategy
identities without any paid calls. It does not change previously frozen plans.
Model explanations, including parameter units, still need independent review.

Use Python 3.12 and uv. Ensure pyproject.toml points to the reviewed Core checkout, then `uv sync --locked --group dev`. Core and Lab must retain compatible runtime bindings. The Core README contains the complete data/research/evidence/reproduction/paper walkthrough.

```powershell
uv run qf-lab --core-root <artifact-root> --lab-root <intelligence-root> data
uv run qf-lab --core-root <artifact-root> --lab-root <intelligence-root> research 'Test a cost-aware IBM SMA hypothesis' --dataset <dataset-id> --program ibm-study
uv run qf-lab --core-root <artifact-root> --lab-root <intelligence-root> inspect ibm-study
uv run qf-lab --core-root <artifact-root> --lab-root <intelligence-root> programs
```

The legacy real provider is DeepSeek deepseek-flash; paid legacy examples require
`--legacy`. New work should use the entrypoints above. Configure DEEPSEEK_API_KEY securely; an existing DSH credential reference is supported without copying the key. Never paste secrets into goals or logs. The gateway offers no tools, validates responses/evidence references and limits calls/tokens/context. Requests persist before network access. Ambiguous failures are retained, never silently retried. `--provider fake` is labelled deterministic test mode.

Two rounds are model-directed. The critic must cite both immutable evidence IDs before freezing/rejecting an existing candidate. Model calls, proposals, decisions and failures are append-only SQLite history. Final holdout values never enter model context. Core metrics are read-only context, never a second authoritative Lab truth store. Exclusive program IDs protect against blind restart.

Run `programs` or `inspect <program>` from another process while research is active. Persisted events show the latest completed stage. IN_PROGRESS_OR_INTERRUPTED deliberately does not claim that a process is still alive. Missing or invalid history produces a structured BLOCKED response; queries never create a new history store.

Checks: `uv run pytest -q`, `uv run ruff check .`, `uv run mypy src`, `git diff --check`. There are no execution tools or generated-code execution. Data and inference limitations remain in Core evidence.

Research Room capacity contention retries only before transport send. The official
transport records a verified unsent reservation race without paid spend; after three
capacity denials the existing program pauses and can resume with its original family
and lifetime budget. Ordinary provider errors and ambiguous sends are never retried.
Declared rooms require their existing call store and matching decision history;
missing or corrupt stores are refused rather than recreated with zero spend.

## Bounded Research Room

The `room` CLI and platform `#research-room` add frozen Researcher/Critic pools,
ordered Director/Validator fallbacks, strict role schemas and append-only call,
budget and memory ledgers. OpenAI-compatible, Anthropic Messages and Gemini
protocols receive credentials transiently. The normal product workflow configures
protected profiles in the platform UI; no key-bearing routing file is necessary.

Each campaign declares at most four rounds/eight trusted Core trials. Core owns
admission, calculations, temporal partitions and immutable EvidencePacks. A dataset
already bound to a research program is rejected before paid calls. Crashed requests
retain their reservations; fallback creates a distinct request, never retries an
ambiguous request. Model opinions remain INTELLIGENCE_MEMORY, separate from facts.

Retained-evidence review performs at most three Critic/Validator/Director calls and
zero new trials. It preserves the original program and source identity and can only
reject or stop. Historical EXACT_MATCH is not current-code reproduction. Research
never grants broker authority, and an honest `NO VALID ALPHA` is a valid conclusion.
