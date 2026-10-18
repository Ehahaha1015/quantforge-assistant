from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from typing import Any

from quantforge.product.context import repository_root
from quantforge.product.lockbox import final_evaluate
from quantforge.product.paper import PaperRuntime

from .test_policy_paper import frozen


def test_actual_process_restart_retains_cursor_and_fills(research_host: Any) -> None:
    runner, _, _ = frozen(research_host)
    final_evaluate(
        runner, program_id="p1", admitted_at=datetime(2025, 1, 1, tzinfo=UTC)
    )

    def command(*args: str) -> dict[str, Any]:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "quantforge.product.cli",
                "--root",
                str(runner.root),
                *args,
            ],
            cwd=repository_root(),
            capture_output=True,
            text=True,
            check=True,
            timeout=45,
        )
        return json.loads(completed.stdout)

    first = command("paper-run", "p1", "--steps", "22")
    assert first["cursor"] == 22 and first["fills"] >= 1
    reopened = command("paper-status", "p1")
    assert reopened == first
    next_process = command("paper-run", "p1", "--steps", "9")
    assert next_process["cursor"] == 31
    paper = PaperRuntime(runner, "p1")
    try:
        fills = [row["fill"]["order_id"] for row in paper.history() if row.get("fill")]
        assert len(fills) == len(set(fills))
        assert command("paper-stop", "p1")["status"] == "STOPPED"
        assert command("paper-step", "p1")["cursor"] == 31
    finally:
        paper.close()
