import json
import sys
from pathlib import Path

import pytest
from test_room import room

from qf_lab.cli import main
from qf_lab.protocols import ProtocolProvider
from qf_lab.quality import quality
from qf_lab.room_gateway import CriticDecision


def test_aliases_expand_only_supplied_ids_and_preserve_invalid_citations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = ProtocolProvider(
        "fixture",
        "OPENAI_COMPATIBLE",
        "https://api.example.com/v1",
        "fixture",
        secret=lambda: {"api_key": "offline-fixture"},
    )
    seen = []
    systems = []

    def generate(system, context, max_output):
        seen.append(context)
        systems.append(system)
        return json.dumps(
            {
                "verdict": "reject",
                "rationale": "Current development evidence is insufficient",
                "evidence_refs": ["E1", "E99"],
                "concerns": [],
            }
        ), {}

    monkeypatch.setattr(provider, "_generate", generate)
    decision, _ = provider.complete_structured(
        {"evidence": [{"evidence_id": "a" * 64}]}, CriticDecision
    )
    assert seen[0]["evidence"][0]["evidence_id"] == "E1"
    assert decision.evidence_refs == ("a" * 64, "E99")


def test_cli_memory_views_and_invalid_routing_are_safe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    app = room(tmp_path, rounds=1, calls=6)
    assert app.run()["status"] == "COMPLETE"
    prefix = [
        "qf-lab",
        "--core-root",
        str(tmp_path / "core"),
        "--lab-root",
        str(tmp_path),
    ]
    for view in ("all", "hypotheses", "failures", "decisions", "strategies", "regimes"):
        monkeypatch.setattr(sys, "argv", [*prefix, "memory", "--program", "room", view])
        main()
        assert (
            json.loads(capsys.readouterr().out)["memory_id"]
            == app.memory()["memory_id"]
        )
    routing = tmp_path / "routing.json"
    routing.write_text("[]")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            *prefix,
            "loop",
            "run",
            "--program",
            "new",
            "--provider",
            "configured",
            "--routing",
            str(routing),
        ],
    )
    with pytest.raises(SystemExit):
        main()
    assert json.loads(capsys.readouterr().out)["reason"] == "ROOM_ROUTING_INVALID"
    assert not (tmp_path / "new").exists()


def test_new_paid_work_does_not_enter_legacy_gateway(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "qf-lab",
            "--core-root",
            str(tmp_path / "core"),
            "--lab-root",
            str(tmp_path / "lab"),
            "research",
            "bounded goal",
            "--dataset",
            "data",
            "--program",
            "program",
        ],
    )
    with pytest.raises(SystemExit):
        main()
    assert (
        json.loads(capsys.readouterr().out)["reason"]
        == "LEGACY_PAID_ENTRY_REQUIRES_EXPLICIT_MODE_USE_ROOM"
    )
    assert not (tmp_path / "core").exists()


def test_quality_separates_negative_research_from_technical_failure(tmp_path: Path) -> None:
    app = room(tmp_path, rounds=1, calls=6)
    state = app.run()
    result = quality([state])
    assert result["completed_new_research_campaigns"] == 1
    assert result["technical_failures"] == 0
    assert result["unique_evidence_packs"] == 2
    assert result["repeated_dataset_strategy_trials"] == 0
    assert result["alpha_status"] == "NOT_ESTABLISHED"
