"""Independent host accounting journal reduced by public Core accounting.

The opening cash/evidence anchor and factual fills are host inputs, never copied
from a dispatch-time broker observation. Hashes attest local identity, not vendor
authentication; production onboarding must qualify the opening anchor separately.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from quantforge.domain import Fill
from quantforge.frontier import ExecutionTargetBinding
from quantforge.portfolio import PortfolioState, apply_fill, create_portfolio

from .storage import Ledger, PlatformError, digest


@dataclass(frozen=True)
class CoreAccountingSnapshot:
    target: ExecutionTargetBinding
    portfolio: PortfolioState
    journal_commitment: str
    portfolio_commitment: str
    anchor_evidence_hash: str


class CoreAccountingBook:
    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.ledger = Ledger(root)

    @staticmethod
    def _target(target: ExecutionTargetBinding) -> dict[str, str]:
        return {
            k: v
            for k, v in target.model_dump(mode="json").items()
            if k != "submission_id"
        }

    @staticmethod
    def _hash(value: str) -> None:
        if not re.fullmatch(r"[a-f0-9]{64}", value):
            raise PlatformError("ACCOUNTING_EVIDENCE_REQUIRED")

    def declare(
        self, target: ExecutionTargetBinding, initial_cash: Decimal, anchor_hash: str
    ) -> None:
        self._hash(anchor_hash)
        portfolio = create_portfolio(initial_cash)
        with self.ledger.transaction() as db:
            if self.ledger.history(db):
                raise PlatformError("ACCOUNTING_ALREADY_DECLARED")
            self.ledger.append(
                db,
                kind="OPENING_CASH",
                target=self._target(target),
                initial_cash=str(portfolio.initial_cash),
                anchor_hash=anchor_hash,
                trust="HOST_ATTESTED_OPENING_ANCHOR_NOT_EXTERNAL_AUTHENTICATION",
            )

    def snapshot(self, target: ExecutionTargetBinding) -> CoreAccountingSnapshot:
        history = self.ledger.read()
        if not history or history[0]["kind"] != "OPENING_CASH":
            raise PlatformError("INDEPENDENT_CORE_ACCOUNTING_REQUIRED")
        opening = history[0]
        if opening["target"] != self._target(target):
            raise PlatformError("ACCOUNTING_TARGET_MISMATCH")
        portfolio = create_portfolio(Decimal(opening["initial_cash"]))
        for row in history[1:]:
            if row["kind"] != "FACTUAL_FILL":
                raise PlatformError("ACCOUNTING_EVENT_INVALID")
            portfolio = apply_fill(portfolio, Fill.model_validate(row["fill"]))
        return CoreAccountingSnapshot(
            target,
            portfolio,
            history[-1]["event_hash"],
            digest(portfolio.model_dump(mode="json")),
            opening["anchor_hash"],
        )

    def record_fill(
        self,
        target: ExecutionTargetBinding,
        fill_id: str,
        fill: Fill,
        evidence_hash: str,
    ) -> None:
        self._hash(evidence_hash)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", fill_id):
            raise PlatformError("ACCOUNTING_FILL_ID_INVALID")
        with self.ledger.transaction() as db:
            history = self.ledger.history(db)
            if not history or history[0]["target"] != self._target(target):
                raise PlatformError("ACCOUNTING_TARGET_MISMATCH")
            payload = fill.model_dump(mode="json")
            existing = next((v for v in history if v.get("fill_id") == fill_id), None)
            if existing:
                if (
                    existing["fill"] != payload
                    or existing["evidence_hash"] != evidence_hash
                ):
                    raise PlatformError("ACCOUNTING_FILL_CONFLICT")
                return
            # Replay inside the transaction so chronology/short constraints are
            # validated before the append; concurrent fills cannot bypass them.
            portfolio = create_portfolio(Decimal(history[0]["initial_cash"]))
            for row in history[1:]:
                portfolio = apply_fill(portfolio, Fill.model_validate(row["fill"]))
            apply_fill(portfolio, fill)
            self.ledger.append(
                db,
                kind="FACTUAL_FILL",
                fill_id=fill_id,
                fill=payload,
                evidence_hash=evidence_hash,
            )
