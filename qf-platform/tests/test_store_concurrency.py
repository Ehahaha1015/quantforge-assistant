"""Concurrent cold-start access must not race on the WAL shared-memory files.

SQLite creates a WAL database's ``-wal``/``-shm`` files on first use and
deletes them when the last connection closes. These stores open a fresh
connection per operation, so when several threads start one at the same instant
they race to create those files and all but one fail with ``SQLITE_READONLY``
("attempt to write a readonly database"). Observed as one successful request
out of four concurrent uploads. The stores now serialise connection creation,
which does not keep the database file open. See
docs/WAL-CONNECTION-LIFETIME-20261016.md.
"""

from __future__ import annotations

import base64
import io
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from PIL import Image

from qf_platform.chart_analysis import ChartAnalysisService
from qf_platform.storage import Ledger


def chart_payload(index: int) -> dict[str, Any]:
    stream = io.BytesIO()
    Image.frombytes("RGB", (400, 300), os.urandom(400 * 300 * 3)).save(stream, "PNG")
    return {
        "symbol": f"00000{index}",
        "period_start": "2026-09-01",
        "period_end": "2026-09-30",
        "image_base64": base64.b64encode(stream.getvalue()).decode(),
    }


def _run_together(work: list[Any], action: Any) -> list[Any]:
    barrier = threading.Barrier(len(work))

    def run(item: Any) -> Any:
        barrier.wait()
        return action(item)

    with ThreadPoolExecutor(max_workers=len(work)) as executor:
        return list(executor.map(run, work))


def test_store_database_uses_write_ahead_logging(tmp_path: Path) -> None:
    charts = ChartAnalysisService(tmp_path)
    with sqlite3.connect(charts.path) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_store_does_not_hold_the_database_file_open(tmp_path: Path) -> None:
    """Serialising connection creation must not pin the file: the store file
    stays replaceable, which the view-cache redirect path relies on."""
    charts = ChartAnalysisService(tmp_path)
    charts.list()
    replaced = charts.root / "replaced.sqlite"
    charts.path.rename(replaced)
    assert replaced.is_file()
    replaced.rename(charts.path)
    assert charts.path.is_file()


def test_concurrent_cold_start_ledger_transactions(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)

    def append(index: int) -> int:
        with ledger.transaction() as db:
            return int(Ledger.append(db, kind="CONCURRENCY", worker=index)["seq"])

    assert sorted(_run_together(list(range(8)), append)) == list(range(1, 9))


def test_concurrent_cold_start_uploads_all_reach_the_store(tmp_path: Path) -> None:
    charts = ChartAnalysisService(tmp_path)
    rounds = 5

    def create(item: tuple[int, int]) -> str:
        round_index, index = item
        created = charts.create(
            chart_payload(round_index * 4 + index), f"c-{round_index}-{index}"
        )
        return str(created["id"])

    accepted: list[str] = []
    for round_index in range(rounds):
        batch = [(round_index, index) for index in range(4)]
        accepted += _run_together(batch, create)
    assert len(set(accepted)) == rounds * 4
    assert len(charts.list()["items"]) == rounds * 4


def test_concurrent_reads_and_writes_share_one_store(tmp_path: Path) -> None:
    """Reads open the same database, so they take part in the same race."""
    charts = ChartAnalysisService(tmp_path)
    for index in range(4):
        charts.create(chart_payload(index), f"seeded-{index}")
    identifiers = [item["id"] for item in charts.list()["items"]]

    def touch(index: int) -> None:
        if index % 2:
            charts.get(identifiers[index % len(identifiers)])
        else:
            charts.create(chart_payload(100 + index), f"mixed-{index}")

    _run_together(list(range(12)), touch)
    assert len(charts.list()["items"]) == 10


def test_independent_instances_preserve_one_idempotent_upload(tmp_path: Path) -> None:
    stores = [ChartAnalysisService(tmp_path) for _ in range(4)]
    value = chart_payload(0)

    def submit(index: int) -> str:
        job = stores[index].create(value, "same-across-instances")
        assert stores[(index + 1) % 4].get(job["id"]) == job
        return str(job["id"])

    identifiers = _run_together(list(range(4)), submit)
    assert len(set(identifiers)) == 1
    assert len(stores[0].list()["items"]) == 1
    assert all(store.status()["calls_used"] == 0 for store in stores)
