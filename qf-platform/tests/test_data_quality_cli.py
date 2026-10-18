from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from qf_platform.data_quality_cli import _frame, fetch_source


def test_vendor_ohlcv_column_contract_is_not_reordered() -> None:
    frame = _frame(
        "159915", [["2021-02-09", "3.001", "3.020", "3.050", "2.990", "123"]]
    )
    row = frame.row(0, named=True)
    assert row["timestamp"] == datetime(2021, 2, 9, tzinfo=UTC)
    assert row["open"] == 3.001
    assert row["close"] == 3.020
    assert row["high"] == 3.050
    assert row["low"] == 2.990
    assert row["volume"] == 123


def test_public_source_fetch_refuses_untrusted_scheme(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        fetch_source("http://push2his.eastmoney.com/foo", tmp_path, "bad")
    with pytest.raises(ValueError):
        fetch_source("https://unknown.example/foo", tmp_path, "bad")
