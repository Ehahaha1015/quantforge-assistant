"""Owner-authorized project notes and verified facts for local retrieval."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from qf_lab.retrieval import KnowledgeCorpus, KnowledgeDocument, Visibility

from .dataset_use import WORKSPACE
from .research_archive import ResearchArchive
from .storage import digest

CORPUS_ROOT = WORKSPACE / "local-smoke/research-governance/knowledge-v1"
PERMISSION: Literal["OWNER_AUTHORIZED_LOCAL_PROJECT_CONTENT"] = (
    "OWNER_AUTHORIZED_LOCAL_PROJECT_CONTENT"
)

NOTES = (
    (
        "sma",
        "SMA causal baseline",
        "SMA moving_average_crossover 均线",
        "SMA uses past closes only. Exact integer windows satisfy 1<=short<long<=50 "
        "in the bounded task facade. A signal executes at next bar open; same-close "
        "execution leaks information. Fractional 0.5 is forbidden in the old binary "
        "contract; weighted replay is a separate version.",
        "quantforge/src/quantforge/strategies/models.py",
    ),
    (
        "momentum",
        "Momentum percent units",
        "momentum threshold_pct 动量 百分比",
        "Momentum uses a past return lookback. In the bounded facade lookback is "
        "integer 2..49. threshold_pct is a plain decimal STRING, not a float or "
        "fraction: '1' means 1 percent; '0.5' means half a percent. "
        "No model-computed performance is an input.",
        "qf-lab/src/qf_lab/bounded_planner.py",
    ),
    (
        "fees",
        "Basis points and declared friction",
        "bps fee percent fraction 基点 费用",
        "One basis point is 0.01 percent or 0.0001 fraction. Thus 10 bps=0.10 "
        "percent=0.001 fraction; 30 bps=0.30 percent. The Core task facade fixes "
        "10 bps per position switch, not authenticated broker fees. "
        "Nonzero costs and next-open timing are shared.",
        "quantforge/src/quantforge/product/service.py",
    ),
    (
        "exposure",
        "Exposed data and final access",
        "exposed final dataset ETF 暴露 锁箱",
        "The complete 510300,510500,159915 2020..2026 intervals were already viewed. "
        "A new directory, model or parameter cannot erase exposure. Final-protected "
        "documents and final price access are never exposed to agents. "
        "Correlated same-market ETFs are not independent confirmations.",
        "docs/DATA-LT-003-ACCEPTANCE.md",
    ),
    (
        "boundary",
        "Host facts and denied authority",
        "credentials live shell metrics tools 权限",
        "The model proposes intent only. The host owns source validation, tool "
        "admission, Core metrics and immutable evidence. Broker credentials, "
        "generated-code execution, shell commands, editing a raw fact store, final "
        "access and live orders must be refused. Retrieval grants no permissions.",
        "qf-platform/AGENTS.md",
    ),
    (
        "budget",
        "Lower budget and exact integers",
        "budget calls cost experiment quota 预算",
        "Each real task allows at most one model call and 2,4,6 maximum Core "
        "experiments. Requested counts are exact integers, not booleans or floats. "
        "The goal USD1 cap and existing lower profile cap both bind; reservations "
        "and failures remain spent. Check requested<=remaining before dispatch; "
        "no top-ups or resets.",
        "docs/AGENT-LT-001-ACCEPTANCE.md",
    ),
    (
        "recovery",
        "Reconcile before replay",
        "unknown timeout retry idempotency resume recover 恢复",
        "Unknown model transport outcomes are parked, never blindly resent. "
        "Confirmed model responses are reused without paying again. Completed "
        "same-input Core runs are inspected and reused; incomplete reservations "
        "need effect review. Unknown HTTP create responses use the original "
        "idempotency key. Changed source identity pauses. Failed pure metadata "
        "checks recover only after source revalidation.",
        "qf-platform/src/qf_platform/agent_runtime.py",
    ),
    (
        "metrics",
        "Metrics and evidence scope",
        "return drawdown Sharpe alpha qualification 指标",
        "Fraction return 0.0365 displays as 3.65 percent. Sharpe is a ratio, not "
        "percent or return. One-bar windows cannot estimate volatility or Sharpe. "
        "Positive historical return, passed screens and repeated runs do not "
        "establish alpha. Numeric quotations select exact source claims with "
        "units and basis; unavailable values remain insufficient.",
        "docs/AGENT-LT-001-ACCEPTANCE.md",
    ),
    (
        "sessions",
        "Missing and partial sessions",
        "159915 suspension gap calendar 停牌 会话",
        "Issuer evidence classifies 159915 2021-02-08 as documented suspension: "
        "do not fabricate zero-return bars. 2021-02-09 was partial; a normal exact "
        "opening fill is not qualified. Unknown gaps remain unresolved; "
        "UTC daily labels are not authenticated intraday timestamps.",
        "docs/DATA-LT-001-ACCEPTANCE.md",
    ),
    (
        "paper",
        "Real forward observation",
        "paper forward eight weeks replay 前向 纸面",
        "Frozen paper strategies require 8..12 real elapsed weeks of subsequent "
        "observations. Historical replay tests software but cannot substitute "
        "for elapsed forward time. Missing quotes, revisions or unknown effects "
        "pause work. Duration alone cannot establish alpha or authorize live funds.",
        "docs/LONG-TERM-OPTIMIZATION-ROADMAP-20261013.md",
    ),
)


def _source_hash(relative: str) -> str:
    path = WORKSPACE / relative
    if path.is_symlink() or not path.is_file():
        raise ValueError("local project note source unavailable")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_corpus(
    root: Path = CORPUS_ROOT, *, archive: ResearchArchive | None = None
) -> KnowledgeCorpus:
    if (root / "corpus.json").exists():
        return KnowledgeCorpus(root)
    available = datetime.now(UTC)
    documents = []
    for id, title, keywords, text, relative in NOTES:
        claims: tuple[dict[str, str], ...] = ()
        if id == "fees":
            claims = (
                {
                    "id": "fee10fraction",
                    "value": "0.001",
                    "unit": "fraction",
                    "basis": "DECLARED_SWITCH",
                },
                {
                    "id": "fee30percent",
                    "value": "0.30",
                    "unit": "percent",
                    "basis": "DECLARED_SWITCH",
                },
            )
        documents.append(
            KnowledgeDocument(
                id,
                title,
                text,
                tuple(keywords.split()),
                available,
                "DEVELOPMENT",
                PERMISSION,
                _source_hash(relative),
                relative,
                claims,
            )
        )
    reports = ResearchArchive() if archive is None else archive
    for symbol in ("510300", "510500", "159915"):
        rows, identity = [], None
        for scenario in ("10-0", "30-1"):
            report = reports.get("trend-suite-v1", asset=symbol, scenario=scenario)
            identity = report["result_hash"]
            for strategy in ("buffered_momentum", "baseline_momentum_20"):
                row = next(
                    value for value in report["rows"] if value["strategy"] == strategy
                )
                id = (
                    ("buffer" if strategy == "buffered_momentum" else "momentum20")
                    + symbol
                    + ("10" if scenario == "10-0" else "30delay")
                )
                rows.append(
                    {
                        "id": id,
                        "value": str(row["metrics"]["total_return"]),
                        "unit": "fraction",
                        "basis": "RAW_PRICE_ONLY_EXPOSED",
                    }
                )
        body = (
            f"ETF {symbol} frozen trend facts; fee/delay are not broker calibration. "
            "Dates exposed. "
            + " ".join(
                f"{value['id']}={value['value']} {value['unit']} "
                f"basis={value['basis']}."
                for value in rows
            )
        )
        documents.append(
            KnowledgeDocument(
                "report" + symbol,
                f"ETF {symbol} trend report",
                body,
                (symbol, "buffered_momentum", "momentum20", "return", "缓冲动量"),
                available,
                "DEVELOPMENT",
                PERMISSION,
                str(identity),
                "Frozen trend-suite-v1/result.json verified by host archive",
                tuple(rows),
            )
        )
    path = (
        WORKSPACE
        / "local-smoke/long-term-20261013/dividends/run-c3917c7cd87b/result.json"
    )
    dividend = json.loads(path.read_bytes())
    if (
        digest(dividend)
        != json.loads(path.with_name("result-digest.json").read_bytes())["sha256"]
    ):
        raise ValueError("cash source result changed")
    if (
        Decimal(dividend["dividend_cash"]) != 491
        or Decimal(dividend["shares"]) != 1000
        or dividend["total_return_qualified"] is not False
    ):
        raise ValueError("frozen cash claim or qualification differs from source")
    cash_claims = (
        {
            "id": "knowncash1000",
            "value": "491",
            "unit": "CNY",
            "basis": "KNOWN_CASH_CONSTANT_1000_SHARES",
        },
    )
    documents.append(
        KnowledgeDocument(
            "cash",
            "Known dividend cash, not full qualification",
            "510300 six retained issuer notices give known cash 491 CNY for "
            "constant 1000 shares; no reinvestment. Raw prices plus cash and "
            "receivables prevent double counting. Full corporate-action coverage "
            "is unverified. Known cash cannot establish full total-return or live "
            "qualification. Claim knowncash1000=491 CNY.",
            ("510300", "dividend", "cash", "分红"),
            available,
            "DEVELOPMENT",
            PERMISSION,
            digest(dividend),
            "Cash result and six source-bound issuer notices",
            cash_claims,
        )
    )
    documents.append(
        KnowledgeDocument(
            "memory",
            "Historical opinion, not a fact",
            "Historical note: slower or buffered momentum reduced turnover in "
            "some assets but did not improve every ETF. Do not repeat a past "
            "preference as a proven new-market conclusion. Unsupported optimism "
            "is opinion. Remembered favorites never grant final access or live "
            "permission.",
            ("memory", "opinion", "buffered_momentum", "记忆"),
            available,
            "OPINION",
            PERMISSION,
            _source_hash("docs/TREND-REFINEMENT-20261013.md"),
            "Project historical research opinion",
        )
    )
    return KnowledgeCorpus.publish(root, tuple(documents))


def query_corpus(
    query: str, *, as_of: datetime, include_memory: bool = False, limit: int = 3
) -> dict[str, Any]:
    return build_corpus().search(
        query, Visibility(as_of, include_memory=include_memory), limit=limit
    )
