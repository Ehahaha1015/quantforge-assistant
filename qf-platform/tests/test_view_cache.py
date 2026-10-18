from __future__ import annotations

import concurrent.futures
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from qf_platform.server import make_server
from qf_platform.storage import PlatformError
from qf_platform.tasks import TaskService
from qf_platform.view_cache import REUSED_SCOPE


def service(root: Path) -> tuple[TaskService, str]:
    root.mkdir(exist_ok=True)
    app = TaskService(root, cache_views=True)
    created = app.create(
        {
            "mode": "OFFLINE_DEMO",
            "dataset": "synthetic-regimes-v1",
            "goal": "Synthetic generation cache only.",
            "max_experiments": 2,
            "analysis": "strategy_comparison",
        },
        "fixture",
    )
    return app, created["id"]


def test_same_generation_reuses_only_prior_full_audit_and_copies_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, task = service(tmp_path)
    first = app.view(task)
    assert first["verification_details"]["method"] == "FULL_SCAN"
    original = app._latest_row
    calls = []

    def checked(db: sqlite3.Connection, identifier: str) -> dict[str, Any]:
        calls.append(identifier)
        return original(db, identifier)

    monkeypatch.setattr(app, "_latest_row", checked)
    warm = app.view(task)
    assert warm["verification_scope"] == REUSED_SCOPE and not calls
    warm["state"]["status"] = "FORGED_CALLER_COPY"
    assert app.view(task)["state"]["status"] == first["state"]["status"]
    deep = app.view(task, deep_audit=True)
    assert len(calls) == 1 and deep["verification_details"]["method"] == "FULL_SCAN"
    app.close_view_cache()


def test_external_append_invalidates_and_reaudits_new_head(tmp_path: Path) -> None:
    app, task = service(tmp_path)
    before = app.view(task)
    other = TaskService(tmp_path)
    other.tick()
    after = app.view(task)
    assert after["head_seq"] > before["head_seq"]
    assert after["verification_details"]["method"] == "FULL_SCAN"
    assert app.view(task)["verification_scope"] == REUSED_SCOPE
    app.close_view_cache()


def test_list_polling_reuses_full_visible_task_audit_and_invalidates_on_commit(
    tmp_path: Path,
) -> None:
    app, task = service(tmp_path)
    first = app.list()
    assert first["verification_details"]["method"] == "FULL_SCAN"
    warm = app.list()
    assert warm["verification_scope"] == REUSED_SCOPE
    assert warm["items"] == first["items"]
    TaskService(tmp_path).tick()
    after = app.list()
    assert after["verification_details"]["method"] == "FULL_SCAN"
    assert after["items"][0]["version"] > first["items"][0]["version"]
    assert app.view(task)["state"]["version"] == after["items"][0]["version"]
    assert app.list()["verification_scope"] == REUSED_SCOPE
    app.close_view_cache()


def test_same_head_early_prefix_mutation_cannot_use_cached_state(
    tmp_path: Path,
) -> None:
    app, task = service(tmp_path)
    app.tick()
    first = app.view(task)
    with sqlite3.connect(app.path) as db:
        db.execute("DROP TRIGGER events_no_update")
        db.execute(
            "UPDATE events SET payload=replace(payload,'CREATED','FORGED') "
            "WHERE task_id=? AND seq=1",
            (task,),
        )
        assert (
            db.execute(
                "SELECT hash FROM events WHERE task_id=? ORDER BY seq DESC LIMIT 1",
                (task,),
            ).fetchone()[0]
            == first["head_hash"]
        )
    with pytest.raises(PlatformError, match="INTEGRITY"):
        app.view(task)
    app.close_view_cache()


def test_old_blob_mutation_invalidates_before_reuse(tmp_path: Path) -> None:
    app, task = service(tmp_path)
    app.tick()
    app.view(task)
    with sqlite3.connect(app.path) as db:
        db.execute("DROP TRIGGER state_blobs_no_update")
        db.execute("UPDATE state_blobs SET payload='{}'")
    with pytest.raises(PlatformError, match="BLOB"):
        app.view(task)
    app.close_view_cache()


def test_cold_scan_does_not_reserve_writer_and_race_is_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, task = service(tmp_path)
    original = app._latest_row
    pinned, release = threading.Event(), threading.Event()

    def scan(db: sqlite3.Connection, identifier: str) -> dict[str, Any]:
        db.execute("SELECT count(*) FROM events").fetchone()
        pinned.set()
        assert release.wait(15)
        return original(db, identifier)

    monkeypatch.setattr(app, "_latest_row", scan)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(app.view, task)
        try:
            assert pinned.wait(5)
            TaskService(tmp_path).tick()
        finally:
            release.set()
        first = future.result(timeout=20)
    monkeypatch.setattr(app, "_latest_row", original)
    after = app.view(task)
    assert after["head_seq"] > first["head_seq"]
    assert after["verification_details"]["method"] == "FULL_SCAN"
    app.close_view_cache()


def test_missing_file_or_redirect_is_not_a_cache_hit(tmp_path: Path) -> None:
    app, task = service(tmp_path)
    app.view(task)
    app.close_view_cache()
    app.path.rename(tmp_path / "old.sqlite")
    with pytest.raises(PlatformError, match="MISSING"):
        app.view(task)


def test_close_drops_generation_and_forces_a_fresh_audit(tmp_path: Path) -> None:
    app, task = service(tmp_path)
    app.view(task)
    assert app.view(task)["verification_scope"] == REUSED_SCOPE
    app.close_view_cache()
    assert app.view(task)["verification_details"]["method"] == "FULL_SCAN"
    app.close_view_cache()


def test_http_default_generation_scope_and_explicit_deep_audit(tmp_path: Path) -> None:
    app, task = service(tmp_path)
    server = make_server(tmp_path, 0, embedded_scheduler=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/api/tasks/{task}/view"
    try:

        def read(suffix: str = "") -> dict[str, Any]:
            with urlopen(url + suffix, timeout=15) as response:
                return json.loads(response.read())

        assert read()["verification_details"]["method"] == "FULL_SCAN"
        assert read()["verification_scope"] == REUSED_SCOPE
        assert read("?audit=full")["verification_details"]["method"] == "FULL_SCAN"
        with pytest.raises(HTTPError) as refusal:
            read("?audit=head")
        assert refusal.value.code == 409
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        app.close_view_cache()


def test_committed_deletion_of_old_prefix_never_returns_cached_head(
    tmp_path: Path,
) -> None:
    app, task = service(tmp_path)
    app.tick()
    app.view(task)
    with sqlite3.connect(app.path) as db:
        db.execute("DROP TRIGGER events_no_delete")
        db.execute("DELETE FROM events WHERE task_id=? AND seq=1", (task,))
    with pytest.raises(PlatformError, match="INTEGRITY"):
        app.view(task)
    app.close_view_cache()


@pytest.mark.parametrize("kind", ["view", "list", "deep"])
@pytest.mark.parametrize("warmed", [False, True])
def test_busy_writer_falls_back_to_full_read_snapshot_and_commit_invalidates(
    tmp_path: Path, kind: str, warmed: bool
) -> None:
    app, task = service(tmp_path)

    def read() -> dict[str, Any]:
        if kind == "list":
            return app.list()
        return app.view(task, deep_audit=kind == "deep")

    def version(value: dict[str, Any]) -> int:
        if kind == "list":
            return int(value["items"][0]["version"])
        return int(value["head_seq"])

    if warmed:
        read()
    with app.connect() as writer:
        writer.execute("BEGIN IMMEDIATE")
        # The reader must finish before this transaction releases its lock.
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            snapshot = pool.submit(read).result(timeout=5)
        assert snapshot["verification_details"]["method"] == "FULL_SCAN"
        assert snapshot.get("verification_scope") != REUSED_SCOPE
        state = app._latest_row(writer, task)["state"]
        app._append(writer, state, "TEST_APPEND", "Concurrent test append")
    after = read()
    assert version(after) == version(snapshot) + 1
    assert after["verification_details"]["method"] == "FULL_SCAN"
    if kind != "deep":
        assert read()["verification_scope"] == REUSED_SCOPE
    app.close_view_cache()
