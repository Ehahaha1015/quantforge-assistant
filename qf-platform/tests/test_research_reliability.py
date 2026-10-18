import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from qf_platform.bindings import BindingRegistry
from qf_platform.research_cli import main
from qf_platform.research_room import RoomService
from qf_platform.storage import PlatformError


def test_wall_budget_denied_before_model_or_core_trials(tmp_path: Path) -> None:
    registry = BindingRegistry(tmp_path)
    service = RoomService(tmp_path, registry)
    with pytest.raises(PlatformError, match="ROOM_DECLARATION_INVALID"):
        service.start({"mode": "SYNTHETIC", "rounds": 1, "calls": 6, "wall_seconds": 0})
    assert not registry.model_usage()
    assert service.current is None


def test_declared_wall_and_memory_are_shared_by_platform_room(tmp_path: Path) -> None:
    service = RoomService(tmp_path, BindingRegistry(tmp_path))
    state = service.start(
        {"mode": "SYNTHETIC", "rounds": 1, "calls": 6, "wall_seconds": 300}
    )
    assert state["declaration"]["research_memory"]["source_count"] == 0
    assert service.worker is not None
    service.worker.join(timeout=120)
    assert service.snapshot()["status"] == "COMPLETE"


@pytest.mark.parametrize("status", ["FAILED", "PAUSED", "STOPPED"])
def test_cli_incomplete_research_returns_nonzero_with_retained_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], status: str,
) -> None:
    BindingRegistry(tmp_path)
    fake = SimpleNamespace(
        start=lambda payload: None,
        worker=SimpleNamespace(join=lambda: None),
        snapshot=lambda: {"status": status, "calls_retained": True},
    )
    monkeypatch.setattr("qf_platform.research_cli.RoomService", lambda *args: fake)
    monkeypatch.setattr(
        sys, "argv",
        [
            "qf-research", "--root", str(tmp_path), "run",
            "--dataset", "fixture", "--goal", "qualification",
        ],
    )
    with pytest.raises(SystemExit) as exited:
        main()
    assert exited.value.code == 2
    assert json.loads(capsys.readouterr().out) == {
        "status": status, "calls_retained": True,
    }
