from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from quantforge.domain import Fill
from quantforge.frontier import ExecutionTargetBinding
from quantforge.portfolio import apply_fill, create_portfolio

from qf_platform.accounting_book import CoreAccountingBook
from qf_platform.storage import PlatformError


def test_accounting_replay_restart_fill_identity_and_target_are_independent(
    tmp_path: Path,
) -> None:
    target = ExecutionTargetBinding(
        schema="frontier-target-v1",
        submission_id="broker1-" + "a" * 32,
        broker="fixture",
        environment="PAPER",
        account_reference="redacted-fixture",
        adapter_id="offline",
        adapter_version="1",
    )
    book = CoreAccountingBook(tmp_path / "accounting")
    with pytest.raises(PlatformError, match="ACCOUNTING_REQUIRED"):
        book.snapshot(target)
    book.declare(target, Decimal("100"), "a" * 64)
    opening = book.snapshot(target)
    fill = Fill(
        order_id="offline-order",
        symbol="FIXTURE",
        timestamp=datetime(2030, 1, 1, tzinfo=UTC),
        side="BUY",
        quantity=Decimal("10"),
        price=Decimal("1"),
        fee=Decimal("2"),
    )
    book.record_fill(target, "fill1", fill, "b" * 64)
    result = book.snapshot(target)
    assert result.portfolio == apply_fill(create_portfolio(100), fill)
    assert result.journal_commitment != opening.journal_commitment
    book.record_fill(target, "fill1", fill, "b" * 64)
    assert CoreAccountingBook(tmp_path / "accounting").snapshot(target) == result
    next_target = target.model_copy(update={"submission_id": "broker1-" + "b" * 32})
    assert book.snapshot(next_target).portfolio == result.portfolio
    with pytest.raises(PlatformError, match="FILL_CONFLICT"):
        book.record_fill(target, "fill1", fill, "c" * 64)
    other = target.model_copy(update={"account_reference": "other-redacted"})
    with pytest.raises(PlatformError, match="TARGET_MISMATCH"):
        book.snapshot(other)
