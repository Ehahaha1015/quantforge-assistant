from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from quantforge.experiments import ExperimentRecord, canonical_json
from quantforge.research.oos import ResearchSealError

from ._fixtures import ADMITTED_AT, Fixture, retained_spec, revised_spec


@pytest.fixture
def host(tmp_path: Path) -> Any:
    fixture = Fixture(tmp_path)
    yield fixture
    fixture.close()


def test_first_use_repeat_denied_exact_recovery_is_not_new_freshness(
    host: Fixture,
) -> None:
    first = host.seal()
    before = host.file_bytes()
    with pytest.raises(ResearchSealError):
        host.seal()
    recovery = host.seal(mode="recovery")
    assert recovery["state"] == "SAME_EXPERIMENT_RECOVERY"
    assert recovery["test_use_allowed"] is True
    assert recovery["fresh_within_retained_history"] is False
    assert recovery["seal"] == first["seal"]
    assert host.file_bytes() == before
    assert host.verify()["state"] == "VERIFIED_NO_TEST_PERMISSION"
    assert host.verify()["test_use_allowed"] is False


def test_close_reopen_preserves_exact_history(host: Fixture) -> None:
    first = host.seal()
    before = host.file_bytes()
    host.reopen()
    assert host.verify()["seal"] == first["seal"]
    assert host.seal(mode="recovery")["seal"] == first["seal"]
    assert host.file_bytes() == before


@pytest.mark.parametrize(
    "field", ["strategy", "backtest", "code_context", "hypothesis"]
)
def test_same_id_changed_full_spec_cannot_recover(host: Fixture, field: str) -> None:
    host.seal()
    changed = host.spec.recipe[field]
    changed[
        {
            "strategy": "short_window",
            "backtest": "fee_bps",
            "code_context": "git_commit",
            "hypothesis": "statement",
        }[field]
    ] = {
        "strategy": 19,
        "backtest": 11.0,
        "code_context": "b" * 40,
        "hypothesis": "Revised after exposure.",
    }[field]
    spec = revised_spec(host.spec, recipe={field: changed})
    before = host.file_bytes()
    for mode in ("first_use", "recovery"):
        with pytest.raises(ResearchSealError):
            host.seal(spec=spec, mode=mode)
    assert host.file_bytes() == before


@pytest.mark.parametrize("changed", ["time", "context", "mode"])
def test_recovery_requires_explicit_exact_context(host: Fixture, changed: str) -> None:
    host.seal()
    context = host.spec.recipe["code_context"]
    context["git_commit"] = "b" * 40
    args = {
        "time": {"admitted_at": ADMITTED_AT + timedelta(seconds=1)},
        "context": {"code_context": context},
        "mode": {"mode": "automatic"},
    }[changed]
    before = host.file_bytes()
    with pytest.raises(ResearchSealError):
        host.seal(mode="recovery", **args) if changed != "mode" else host.seal(**args)
    assert host.file_bytes() == before


@pytest.mark.parametrize("offset", [-251, 0, 1, 200, 251])
def test_inclusive_test_overlap_rejects_changed_id_dataset_hash_and_config(
    host: Fixture, offset: int
) -> None:
    host.seal()
    spec = retained_spec(
        host.catalog,
        experiment_id="changed.002",
        dataset_id="revision",
        start=datetime(2020, 1, 1, 14, 30, tzinfo=UTC) + timedelta(days=offset),
        price=200.0,
    )
    backtest = spec.recipe["backtest"]
    backtest["fee_bps"] = 11.0
    spec = revised_spec(spec, recipe={"backtest": backtest})
    before = host.file_bytes()
    with pytest.raises(ResearchSealError):
        host.seal(spec=spec)
    assert host.file_bytes() == before
    assert not host.seal_path(spec).exists()


def test_final_test_disjoint_despite_overlapping_development_is_allowed(
    host: Fixture,
) -> None:
    host.seal()
    spec = retained_spec(
        host.catalog,
        experiment_id="disjoint.002",
        dataset_id="disjoint",
        start=datetime(2020, 1, 1, 14, 30, tzinfo=UTC) + timedelta(days=252),
    )
    receipt = host.seal(spec=spec)
    assert receipt["test_use_allowed"] is True
    assert receipt["fresh_within_retained_history"] is True
    assert len(list(host.root.iterdir())) == 2
    assert host.verify()["test_use_allowed"] is False


def test_same_dates_other_symbol_are_a_different_protected_partition(
    host: Fixture,
) -> None:
    host.seal()
    spec = retained_spec(
        host.catalog, experiment_id="other.002", dataset_id="qqq", symbol="QQQ"
    )
    assert host.seal(spec=spec)["fresh_within_retained_history"] is True


@pytest.mark.parametrize(
    "outcome",
    [
        "RESEARCH_SCREEN_PASS",
        "RESEARCH_SCREEN_FAIL",
        "INVALID_RESEARCH",
        "INCOMPLETE",
        "UNSUPPORTED_REQUEST",
    ],
)
def test_terminal_outcomes_including_incomplete_never_reopen_test_use(
    host: Fixture, outcome: str
) -> None:
    host.seal()
    host.save_terminal(outcome=outcome)
    before = host.file_bytes()
    host.reopen()
    receipt = host.seal(mode="recovery")
    assert receipt["state"] == "TERMINAL_NO_TEST_PERMISSION"
    assert receipt["terminal_outcome"] == outcome
    assert receipt["test_use_allowed"] is False
    assert receipt["fresh_within_retained_history"] is False
    assert host.verify()["terminal_outcome"] == outcome
    assert host.file_bytes() == before


@pytest.mark.parametrize(
    "bad",
    [
        "missing_ref",
        "wrong_ref",
        "missing_hash",
        "wrong_hash",
        "wrong_git",
        "wrong_dataset",
        "wrong_seed",
        "wrong_outcome",
        "missing_namespace",
    ],
)
def test_inconsistent_terminal_record_blocks_all_admission(
    host: Fixture, bad: str
) -> None:
    host.seal()
    changes: dict[str, Any] = {}
    if bad == "missing_ref":
        changes["artifact_refs"] = []
    elif bad == "wrong_ref":
        changes["artifact_refs"] = [
            {"name": "research_oos_seal", "uri": "https://invalid.example/ignored"}
        ]
    elif bad == "wrong_git":
        changes["git_commit"] = "b" * 40
    elif bad == "wrong_dataset":
        changes["dataset_hash"] = "b" * 64
    elif bad == "wrong_seed":
        changes["random_seed"] = 1
    else:
        integrity = {} if bad == "missing_hash" else {"research_oos_seal": "0" * 64}
        changes["metrics"] = (
            {}
            if bad == "missing_namespace"
            else {
                "research_factory": {
                    "outcome": "LLM_APPROVED"
                    if bad == "wrong_outcome"
                    else "INCOMPLETE",
                    "artifact_integrity": integrity,
                }
            }
        )
    host.save_terminal(**changes)
    before = host.file_bytes()
    for call in (host.verify, lambda: host.seal(mode="recovery")):
        with pytest.raises(ResearchSealError):
            call()
    assert host.file_bytes() == before


def test_missing_terminal_seal_blocks_even_disjoint_new_experiment(
    host: Fixture,
) -> None:
    host.seal()
    host.save_terminal()
    host.seal_path().unlink()  # simulate host damage; production has no deletion API
    spec = retained_spec(
        host.catalog,
        experiment_id="new",
        dataset_id="new",
        start=datetime(2020, 1, 1, 14, 30, tzinfo=UTC) + timedelta(days=252),
    )
    before = host.file_bytes()
    with pytest.raises(ResearchSealError):
        host.seal(spec=spec)
    assert host.file_bytes() == before


def test_unrelated_store_record_does_not_create_factory_history(host: Fixture) -> None:
    host.store.save(
        ExperimentRecord.create(
            experiment_id="unrelated",
            git_commit="a" * 40,
            dataset_id="retained-SPY",
            dataset_hash="b" * 64,
            backtest_config={"external": True},
            random_seed=0,
            started_at=ADMITTED_AT,
            metrics={},
        )
    )
    assert host.seal()["test_use_allowed"] is True


def test_any_existing_store_id_cannot_be_reused_for_a_first_factory_seal(
    host: Fixture,
) -> None:
    host.store.save(
        ExperimentRecord.create(
            experiment_id=host.spec.experiment_id,
            git_commit="a" * 40,
            dataset_id="retained-SPY",
            dataset_hash="b" * 64,
            backtest_config={"external": True},
            random_seed=0,
            started_at=ADMITTED_AT,
            metrics={},
        )
    )
    before = host.file_bytes()
    with pytest.raises(ResearchSealError):
        host.seal()
    assert host.file_bytes() == before


@pytest.mark.parametrize(
    "bad",
    [
        "unknown_version",
        "guard",
        "state",
        "partition",
        "snapshot",
        "spec_hash",
        "extra",
        "missing",
        "noncanonical",
        "duplicate",
        "nonfinite",
        "truncated",
    ],
)
def test_durable_inconsistent_seal_tamper_is_never_repaired(
    host: Fixture, bad: str
) -> None:
    host.seal()
    path = host.seal_path()
    seal = json.loads(path.read_bytes())
    if bad == "unknown_version":
        seal["schema_version"] = "research-oos-seal-v999"
    elif bad == "guard":
        seal["guard_version"] = "future"
    elif bad == "state":
        seal["exposure_state"] = "UNEXPOSED"
    elif bad == "partition":
        seal["partitions"]["test"]["row_start"] = 0
    elif bad == "snapshot":
        seal["dataset_snapshot"]["content_hash"] = "b" * 64
    elif bad == "spec_hash":
        seal["spec"]["config_hash"] = "b" * 64
    elif bad == "extra":
        seal["metrics"] = {"sharpe": 10}
    elif bad == "missing":
        del seal["prior_exposure"]
    raw = canonical_json(seal)
    if bad == "noncanonical":
        raw += "\n"
    elif bad == "duplicate":
        raw = raw.replace(
            '"guard_version":',
            '"guard_version":"research-oos-guard-v1","guard_version":',
            1,
        )
    elif bad == "nonfinite":
        raw = raw.replace(
            '"schema_version":"research-oos-seal-v1"', '"schema_version":NaN'
        )
    elif bad == "truncated":
        raw = raw[:25]
    path.write_text(raw, encoding="utf-8")
    before = host.file_bytes()
    for call in (host.verify, host.seal, lambda: host.seal(mode="recovery")):
        with pytest.raises(ResearchSealError):
            call()
    assert host.file_bytes() == before
