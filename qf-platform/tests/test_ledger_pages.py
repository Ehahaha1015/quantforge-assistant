import http.client
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from qf_platform.server import make_server
from qf_platform.storage import Ledger, PlatformError


def seeded(root: Path) -> Ledger:
    ledger = Ledger(root)
    with ledger.transaction() as db:
        for index in range(12):
            ledger.append(db, kind="SYNTHETIC_TEST_ONLY", index=index)
    return ledger


def test_pages_fold_exactly_to_full_audited_history(tmp_path: Path) -> None:
    ledger = seeded(tmp_path)
    after, anchor, rows = 0, None, []
    while True:
        page = ledger.page(after=after, anchor=anchor, limit=5)
        assert page["verification_scope"] == "CHAIN_SEGMENT_ONLY_NOT_FULL_AUDIT"
        rows += page["events"]
        after, anchor = page["next_cursor"], page["next_anchor"]
        if not page["has_more"]:
            break
    assert rows == ledger.read()
    assert ledger.page(after=after, anchor=anchor)["events"] == []


def test_cursor_and_changed_anchor_fail_closed(tmp_path: Path) -> None:
    ledger = seeded(tmp_path)
    for cursor in (
        {"after": True},
        {"after": -1},
        {"after": 1},
        {"limit": 501},
        {"after": 1, "anchor": "0" * 64},
    ):
        with pytest.raises(PlatformError):
            ledger.page(**cursor)
    page = ledger.page(limit=5)
    with sqlite3.connect(ledger.path) as db:
        db.execute("DROP TRIGGER deny_update")
        db.execute("UPDATE events SET payload='{}' WHERE seq=5")
    with pytest.raises(PlatformError, match="ANCHOR_CONFLICT"):
        ledger.page(after=5, anchor=page["next_anchor"])


def test_segment_is_not_misrepresented_as_prefix_audit(tmp_path: Path) -> None:
    ledger = seeded(tmp_path)
    page = ledger.page(limit=5)
    with sqlite3.connect(ledger.path) as db:
        db.execute("DROP TRIGGER deny_update")
        db.execute("UPDATE events SET payload='{}' WHERE seq=1")
    # A later segment cannot detect unrelated old bytes; the explicit full audit does.
    assert ledger.page(after=5, anchor=page["next_anchor"])["events"]
    with pytest.raises(PlatformError, match="INTEGRITY_FAILED"):
        ledger.read()


def test_http_cursor_cannot_cross_root_or_omit_anchor(tmp_path: Path) -> None:
    server = make_server(tmp_path, 0, embedded_scheduler=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def get(query: str) -> tuple[int, dict]:
        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            client.request("GET", "/api/history-page?domain=simulation&" + query)
            response = client.getresponse()
            return response.status, json.loads(response.read())
        finally:
            client.close()

    try:
        status, page = get("limit=1")
        assert status == 200 and page["events"]
        after = page["next_cursor"]
        assert get(f"after={after}")[0] == 409
        assert (
            get(f"after={after}&anchor={page['next_anchor']}&root_id=wrong")[0] == 409
        )
        status, next_page = get(
            f"after={after}&anchor={page['next_anchor']}&root_id={page['root_id']}"
        )
        assert status == 200 and next_page["anchor"] == page["next_anchor"]
        assert next_page["verification_scope"] == "CHAIN_SEGMENT_ONLY_NOT_FULL_AUDIT"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
