from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from _support import GOLDEN_ATTEMPT_ID, SUBMISSION_ID, make_observation, make_target

from quantforge.execution_ledger import AttemptPhase
from quantforge.execution_ledger import projection as projection_module
from quantforge.execution_ledger.projection import project_attempt


def _view():
    return project_attempt(
        submission_id=SUBMISSION_ID,
        target=make_target(),
        generation=1,
        recovery_epoch=1,
        execstate_state="DISPATCHING",
        observations=(
            make_observation(
                observation_id="execstate-observation-v1-1"
            ),
        ),
        current_receipt_id="execstate-receipt-v1-2",
    )


def test_views_are_frozen_and_carry_no_mutation_api() -> None:
    view = _view()
    assert view.attempt_id == GOLDEN_ATTEMPT_ID
    with pytest.raises(dataclasses.FrozenInstanceError):
        view.phase = AttemptPhase.FINALIZED  # type: ignore[misc]
    for banned in (
        "save",
        "persist",
        "apply",
        "update",
        "reconcile",
        "transition",
        "record",
    ):
        assert banned not in dir(view)


def test_projection_calls_return_fresh_objects() -> None:
    first = _view()
    second = _view()
    assert first == second
    assert first is not second


def test_projection_inputs_are_unchanged() -> None:
    observation = make_observation(
        observation_id="execstate-observation-v1-1"
    )
    observations = (observation,)
    before = (observation, observations)
    _view()
    assert (observation, observations) == before


def test_projection_module_has_no_persistence_or_execstate_tokens() -> None:
    source = Path(projection_module.__file__).read_text(encoding="utf-8")
    for token in (
        "sqlite3",
        "quantforge.execstate",
        "sidecar",
        "adapter",
        "INSERT",
        "UPDATE",
        "DELETE",
        "commit",
    ):
        assert token not in source, token
