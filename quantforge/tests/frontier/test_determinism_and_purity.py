from __future__ import annotations

import ast
import builtins
import os
import random
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import NoReturn

import pytest
from _frontier_fixtures import complete_inputs

import quantforge.broker as broker_api
import quantforge.broker.submission as broker_submission_api
import quantforge.execution.paper as paper_api
import quantforge.execution.paper.execute as paper_execute_api
import quantforge.portfolio as portfolio_api
import quantforge.portfolio.accounting as portfolio_accounting_api
from quantforge.frontier import FrontierReason, evaluate_frontier_eligibility


@pytest.mark.parametrize("seed", ["random", "0", "12345"])
def test_fresh_process_hash_seed_stability(seed: str) -> None:
    script = Path(__file__).with_name("hash_seed_probe.py")
    environment = os.environ.copy()
    environment["PYTHONHASHSEED"] = seed
    completed = subprocess.run(
        [sys.executable, str(script)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        env=environment,
    )
    expected = evaluate_frontier_eligibility(**complete_inputs()).model_dump_json()
    assert completed.stdout.strip() == expected


def test_evaluation_uses_no_external_or_implicit_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eligible = complete_inputs()
    denied = complete_inputs()
    denied["operator_evidence"] = None

    def fail(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError(f"unexpected side effect: {args!r}, {kwargs!r}")

    class EnvironmentTrap:
        def _fail(self, *args: object, **kwargs: object) -> NoReturn:
            raise AssertionError(f"unexpected os.environ access: {args!r}, {kwargs!r}")

        __getitem__ = _fail
        __contains__ = _fail
        __iter__ = _fail
        __len__ = _fail
        get = _fail
        keys = _fail
        values = _fail
        items = _fail
        copy = _fail

        def __getattr__(self, name: str) -> NoReturn:
            raise AssertionError(f"unexpected os.environ attribute access: {name}")

    with monkeypatch.context() as isolated:
        isolated.setattr(builtins, "open", fail)
        isolated.setattr(socket, "socket", fail)
        isolated.setattr(os, "getenv", fail)
        isolated.setattr(os, "environ", EnvironmentTrap())
        isolated.setattr(subprocess, "Popen", fail)
        isolated.setattr(time, "time", fail)
        isolated.setattr(random, "random", fail)
        isolated.setattr(broker_api, "broker_submission_id", fail)
        isolated.setattr(broker_api, "create_broker_submission_envelope", fail)
        isolated.setattr(broker_submission_api, "broker_submission_id", fail)
        isolated.setattr(
            broker_submission_api, "create_broker_submission_envelope", fail
        )
        isolated.setattr(paper_api, "execute_paper_order", fail)
        isolated.setattr(paper_execute_api, "execute_paper_order", fail)
        isolated.setattr(portfolio_api, "apply_fill", fail)
        isolated.setattr(portfolio_accounting_api, "apply_fill", fail)

        eligible_result = evaluate_frontier_eligibility(**eligible)
        denied_result = evaluate_frontier_eligibility(**denied)

    assert eligible_result.eligible is True
    decision = denied_result
    assert decision.reasons == (FrontierReason.OPERATOR_EVIDENCE_MISSING,)


def test_production_modules_have_no_forbidden_capability_imports_or_calls() -> None:
    root = Path(__file__).parents[2] / "src" / "quantforge" / "frontier"
    forbidden_imports = {
        "asyncio",
        "http",
        "os",
        "pathlib",
        "random",
        "requests",
        "socket",
        "sqlite3",
        "subprocess",
        "time",
        "urllib",
    }
    forbidden_calls = {
        "apply_fill",
        "cancel",
        "create_broker_submission_envelope",
        "open",
        "poll",
        "resend",
        "resubmit",
        "retry",
        "send",
    }
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = {
            node.names[0].name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
        }
        imports.update(
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        )
        calls = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert imports.isdisjoint(forbidden_imports)
        assert calls.isdisjoint(forbidden_calls)
