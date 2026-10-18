from __future__ import annotations

import ast
import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from pydantic import ValidationError
from quantforge.data import BAR_SCHEMA, ParquetDataCatalog

from qf_lab.controller import RoundController, inspect_history, list_programs
from qf_lab.gateway import FakeProvider, GatewayError, ModelGateway, ResearchDecision


def test_gateway_budget_schema_evidence_refs_and_durable_call_history(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    gateway = ModelGateway(tmp_path / "calls.sqlite", provider, max_calls=1)
    context = {
        "phase": "first",
        "goal": "Test SMA",
        "max_long_window": 20,
        "evidence": [],
    }
    call_id, decision = gateway.call(context)
    assert decision.action == "propose"
    assert gateway.ledger()[-1]["real_model_call"] is False
    with pytest.raises(GatewayError, match="BUDGET"):
        gateway.call(context)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        gateway.connection.execute("DELETE FROM calls")
    gateway.close()
    reopened = ModelGateway(tmp_path / "calls.sqlite", provider, max_calls=1)
    assert reopened.ledger()[0]["call_id"] == call_id
    with pytest.raises(GatewayError, match="BUDGET"):
        reopened.call(context)
    reopened.close()


def test_model_cannot_return_authoritative_metrics() -> None:
    payload = {
        "action": "propose",
        "short_window": 5,
        "long_window": 15,
        "rationale": "Some bounded hypothesis",
        "evidence_refs": [],
        "sharpe": 99.0,
    }
    with pytest.raises(ValidationError):
        ResearchDecision.model_validate(payload)


def test_invalid_evidence_reference_is_preserved_as_failure(tmp_path: Path) -> None:
    class ForgedProvider(FakeProvider):
        def complete(self, context: dict[str, Any]) -> Any:
            decision, metadata = super().complete(context)
            return decision.model_copy(
                update={"evidence_refs": ("forged-id",)}
            ), metadata

    gateway = ModelGateway(tmp_path / "calls.sqlite", ForgedProvider())
    try:
        with pytest.raises(GatewayError, match="REFERENCE"):
            gateway.call({"phase": "first", "max_long_window": 20, "evidence": []})
        assert [row["phase"] for row in gateway.ledger()] == ["REQUEST", "ERROR"]
    finally:
        gateway.close()


def test_two_round_loop_changed_parameters_reproduces_and_preserves_history(
    tmp_path: Path,
) -> None:
    core = tmp_path / "core"
    core.mkdir()
    catalog = ParquetDataCatalog(core / "catalog")
    start = datetime(2020, 1, 1, tzinfo=UTC)
    prices = [100.0 + 0.2 * i for i in range(100)]
    frame = pl.DataFrame(
        {
            "symbol": ["IBM"] * 100,
            "timestamp": [start + timedelta(days=i) for i in range(100)],
            "open": prices,
            "high": [v + 2 for v in prices],
            "low": [v - 2 for v in prices],
            "close": [v + 1 for v in prices],
            "volume": [1000.0] * 100,
        },
        schema=BAR_SCHEMA,
    )
    snapshot = catalog.write(
        "synthetic-ibm",
        frame,
        source="synthetic-fixture",
        schema_version="bars-v1",
        as_of=start + timedelta(days=100),
    )
    controller = RoundController(core, tmp_path / "lab", FakeProvider())
    result = controller.run(
        "Test a fixed causal SMA baseline",
        dataset_id=snapshot.dataset_id,
        program_id="testprogram",
    )
    assert result["rounds_completed"] == 2
    assert result["real_calls_proven"] is False
    assert result["frozen"]["candidate_count"] == 2
    history = inspect_history(tmp_path / "lab", "testprogram")
    proposals = [event for event in history if event["kind"] == "PROPOSAL"]
    assert proposals[0]["short_window"] != proposals[1]["short_window"]
    decisions = [event for event in history if event["kind"] == "DECISION"]
    assert decisions[1]["decision"]["evidence_refs"]
    assert history[-1]["kind"] == "COMPLETE"
    progress = list_programs(tmp_path / "lab")
    assert len(progress) == 1
    assert progress[0]["program_id"] == "testprogram"
    assert progress[0]["status"] == "COMPLETE"
    assert progress[0]["rounds_completed"] == 2
    assert inspect_history(tmp_path / "lab", "testprogram") == history
    with pytest.raises(FileExistsError):
        controller.run(
            "same goal", dataset_id=snapshot.dataset_id, program_id="testprogram"
        )
    ledger = Path(result["call_ledger"])
    with sqlite3.connect(ledger) as connection:
        requests = [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT payload FROM calls WHERE phase='REQUEST' ORDER BY seq"
            )
        ]
    assert (
        requests[1]["context"]["evidence"][0]["FACT"]["final_lockbox_scored"] is False
    )
    assert "validation_periods" not in requests[1]["context"]["evidence"][0]["FACT"]
    assert "final_metrics" not in requests[1]["context"]["evidence"][0]["FACT"]


@pytest.mark.parametrize("corrupt", [False, True])
def test_history_cli_blocks_missing_or_corrupt_store_without_creating_history(
    tmp_path: Path, corrupt: bool
) -> None:
    lab = tmp_path / "lab"
    database = lab / "missing" / "history.sqlite"
    if corrupt:
        database.parent.mkdir(parents=True)
        database.write_bytes(b"invalid database")
    before = database.read_bytes() if corrupt else None
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "qf_lab.cli",
            "--core-root",
            str(tmp_path / "core"),
            "--lab-root",
            str(lab),
            "inspect",
            "missing",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert json.loads(completed.stdout)["status"] == "BLOCKED"
    assert "Traceback" not in completed.stderr
    assert (database.read_bytes() if database.exists() else None) == before
    assert not (tmp_path / "core").exists()


def test_history_rejects_redirected_program_directory(tmp_path: Path) -> None:
    lab, outside = tmp_path / "lab", tmp_path / "outside"
    lab.mkdir()
    outside.mkdir()
    with sqlite3.connect(outside / "history.sqlite") as connection:
        connection.execute("CREATE TABLE events(seq INTEGER PRIMARY KEY, payload TEXT)")
        connection.execute(
            "INSERT INTO events(payload) VALUES (?)", ('{"kind":"COMPLETE"}',)
        )
    link = lab / "redirected"
    if sys.platform == "win32":
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"New-Item -ItemType Junction -Path '{link}' -Target '{outside}' | Out-Null",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(GatewayError, match="REDIRECTED"):
        inspect_history(lab, "redirected")
    assert list_programs(lab) == []
    assert inspect_history(tmp_path, "outside") == [{"seq": 1, "kind": "COMPLETE"}]


def test_lab_never_imports_raw_store_or_execution_authority() -> None:
    package = Path(__file__).resolve().parents[1] / "src" / "qf_lab"
    forbidden = {
        "quantforge.experiments",
        "quantforge.broker",
        "quantforge.live",
        "quantforge.credentials",
        "quantforge.execstate",
        "quantforge.execution",
    }
    for path in package.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not any(
                    node.module == v or node.module.startswith(v + ".")
                    for v in forbidden
                ), path
