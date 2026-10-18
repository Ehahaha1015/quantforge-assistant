import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from qf_platform.service import PlatformService
from qf_platform.storage import Ledger, PlatformError


def seeded(root: Path, count: int = 8) -> Ledger:
    ledger = Ledger(root)
    with ledger.transaction() as db:
        for index in range(count):
            ledger.append(db, kind="SYNTHETIC_TEST_ONLY", state={"ordinal": index})
    return ledger


def test_projection_keeps_full_integrity_but_only_requested_tail(
    tmp_path: Path,
) -> None:
    ledger = seeded(tmp_path)
    complete = ledger.read()
    projection = ledger.read_projection(limit=3)
    assert projection["tail"] == complete[-3:]
    assert projection["events_total"] == len(complete)
    assert projection["head_hash"] == complete[-1]["event_hash"]
    assert projection["verification_scope"] == "FULL_CHAIN_AT_READ_SNAPSHOT"
    with sqlite3.connect(ledger.path) as db:
        db.execute("DROP TRIGGER deny_update")
        db.execute("UPDATE events SET payload='{}' WHERE seq=1")
    with pytest.raises(PlatformError, match="INTEGRITY_FAILED"):
        ledger.read_projection(limit=1)


def test_append_audits_once_in_managed_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = seeded(tmp_path)
    original = Ledger._verified_rows
    scans: list[int] = []

    def counted(db: sqlite3.Connection):
        scans.append(1)
        yield from original(db)

    monkeypatch.setattr(Ledger, "_verified_rows", staticmethod(counted))
    with ledger.transaction() as db:
        for _ in range(10):
            ledger.append(db, kind="SYNTHETIC_TEST_ONLY")
    assert len(scans) == 1
    with ledger.transaction() as db:
        ledger.append(db, kind="SYNTHETIC_TEST_ONLY")
    assert len(scans) == 2
    assert len(ledger.read()) == 19


def test_direct_prefix_change_invalidates_managed_head(tmp_path: Path) -> None:
    ledger = seeded(tmp_path)
    with ledger.transaction() as db:
        ledger.append(db, kind="SYNTHETIC_TEST_ONLY")
        db.execute("DROP TRIGGER deny_update")
        db.execute("UPDATE events SET payload='{}' WHERE seq=1")
        with pytest.raises(PlatformError, match="INTEGRITY_FAILED"):
            ledger.append(db, kind="MUST_NOT_APPEND")


def test_savepoint_rollback_does_not_reuse_removed_head(tmp_path: Path) -> None:
    ledger = seeded(tmp_path)
    with ledger.transaction() as db:
        db.execute("SAVEPOINT fixture")
        removed = ledger.append(db, kind="ROLLED_BACK_TEST_ONLY")
        db.execute("ROLLBACK TO fixture")
        current = ledger.append(db, kind="KEPT_TEST_ONLY")
        assert removed["seq"] == current["seq"] == 9
    assert ledger.read()[-1]["kind"] == "KEPT_TEST_ONLY"


def test_external_commit_after_explicit_transaction_boundary_invalidates_head(
    tmp_path: Path,
) -> None:
    ledger = seeded(tmp_path)
    with pytest.raises(PlatformError, match="INTEGRITY_FAILED"):
        with ledger.transaction() as db:
            ledger.append(db, kind="SYNTHETIC_TEST_ONLY")
            db.execute("COMMIT")
            with sqlite3.connect(ledger.path) as other:
                other.execute("DROP TRIGGER deny_update")
                other.execute("UPDATE events SET payload='{}' WHERE seq=1")
            db.execute("BEGIN IMMEDIATE")
            ledger.append(db, kind="MUST_NOT_APPEND")


def test_extra_insert_trigger_is_not_admitted_as_known_single_append(
    tmp_path: Path,
) -> None:
    ledger = seeded(tmp_path)
    with pytest.raises(PlatformError, match="UNEXPECTED_EFFECT"):
        with ledger.transaction() as db:
            db.execute("CREATE TABLE fixture_effects (value INTEGER)")
            db.execute(
                "CREATE TRIGGER fixture_side_effect AFTER INSERT ON events "
                "BEGIN INSERT INTO fixture_effects VALUES(1); END"
            )
            ledger.append(db, kind="SYNTHETIC_TEST_ONLY")
    assert len(ledger.read()) == 8


def test_unmanaged_connections_never_reuse_transaction_cache(tmp_path: Path) -> None:
    ledger = seeded(tmp_path)
    with closing(ledger.connect()) as db, db:
        ledger.append(db, kind="SYNTHETIC_TEST_ONLY")
        db.execute("DROP TRIGGER deny_update")
        db.execute("UPDATE events SET payload='{}' WHERE seq=1")
        with pytest.raises(PlatformError, match="INTEGRITY_FAILED"):
            ledger.append(db, kind="MUST_NOT_APPEND")


def test_simulation_snapshot_matches_reference_full_history(tmp_path: Path) -> None:
    app = PlatformService(tmp_path)
    app.operate("cycle", {}, "fixture-cycle")
    reference, projected = app._snapshot(app.ledger.read()), app.snapshot()
    for value in (reference, projected):
        value.pop("observed_at")
    assert reference == projected
    assert projected["real_transport"] == "LOCKED"
    with sqlite3.connect(app.ledger.path) as db:
        db.execute("DROP TRIGGER deny_update")
        db.execute("UPDATE events SET hash='forged' WHERE seq=1")
    with pytest.raises(PlatformError, match="INTEGRITY_FAILED"):
        app.snapshot()
