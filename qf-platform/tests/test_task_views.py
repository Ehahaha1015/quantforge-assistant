from __future__ import annotations

import json
from pathlib import Path

import pytest

from qf_platform.storage import PlatformError
from qf_platform.tasks import TaskService


def create(root: Path) -> tuple[TaskService, str]:
    root.mkdir(exist_ok=True)
    service = TaskService(root)
    value = service.create(
        {
            "mode": "OFFLINE_DEMO",
            "dataset": "synthetic-regimes-v1",
            "goal": "Bounded event view fixture only",
            "max_experiments": 2,
            "analysis": "strategy_comparison",
        },
        "fixture",
    )
    for _ in range(3):
        service.tick()
    return service, value["id"]


def test_view_matches_full_state_and_fixed_head_pages_survive_append(
    tmp_path: Path,
) -> None:
    service, task = create(tmp_path)
    view = service.view(task)
    full = service.get(task)
    assert "events" not in view["state"]
    for key, value in full.items():
        if key not in {"events", "observed_at"}:
            assert view["state"][key] == value
    assert view["events_total"] == full["version"] == len(full["events"])
    args = {key: view[key] for key in ("root_id", "head_seq", "head_hash")}
    first = service.event_page(task, **args, limit=2)
    service.tick()
    rows = list(first["events"])
    current = first
    while current["has_more"]:
        current = service.event_page(
            task,
            **args,
            after=current["next_cursor"],
            anchor=current["next_anchor"],
            limit=2,
        )
        rows += current["events"]
    assert rows == full["events"]
    assert current["next_cursor"] == view["head_seq"]
    assert current["next_anchor"] == view["head_hash"]
    with pytest.raises(PlatformError, match="ANCHOR_CONFLICT"):
        service.event_page(task, **args, after=2, anchor="f" * 64)
    with pytest.raises(PlatformError, match="ROOT"):
        service.event_page(task, **{**args, "root_id": "f" * 24})
    with pytest.raises(PlatformError, match="HEAD_CONFLICT"):
        service.event_page(task, **{**args, "head_hash": "f" * 64})


def test_full_view_rejects_early_tamper_while_later_segment_does_not_claim_full_audit(
    tmp_path: Path,
) -> None:
    service, task = create(tmp_path)
    view = service.view(task)
    args = {key: view[key] for key in ("root_id", "head_seq", "head_hash")}
    with service.connect() as db:
        db.execute("DROP TRIGGER events_no_update")
        raw = db.execute(
            "SELECT payload FROM events WHERE task_id=? AND seq=1", (task,)
        ).fetchone()[0]
        value = json.loads(raw)
        value["summary"] = "tampered fixture"
        db.execute(
            "UPDATE events SET payload=? WHERE task_id=? AND seq=1",
            (json.dumps(value), task),
        )
    with pytest.raises(PlatformError, match="INTEGRITY"):
        service.view(task)
    full = view["head_seq"]
    # An unvisited old row is outside the segment guarantee; do not silently broaden it.
    page = service.event_page(task, **args, after=full, anchor=view["head_hash"])
    assert page["verification_scope"] == "CHAIN_SEGMENT_AND_CONTENT_NOT_FULL_AUDIT"
    assert not page["events"]


def test_page_rejects_missing_old_content_and_invalid_cursors(tmp_path: Path) -> None:
    service, task = create(tmp_path)
    view = service.view(task)
    args = {key: view[key] for key in ("root_id", "head_seq", "head_hash")}
    for cursor in (-1, True, view["head_seq"] + 1):
        with pytest.raises(PlatformError):
            service.event_page(task, **args, after=cursor)
    with service.connect() as db:
        db.execute("DROP TRIGGER state_blobs_no_delete")
        db.execute("DELETE FROM state_blobs")
    with pytest.raises(PlatformError):
        service.view(task)
    with pytest.raises(PlatformError):
        service.event_page(task, **args)
