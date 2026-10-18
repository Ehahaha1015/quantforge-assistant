"""PRICE-001 public API discipline (contract sections 18, 20 and 21)."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from quantforge import marketdata
from quantforge.marketdata import (
    PriceArgumentError,
    PriceCoverageError,
    PriceEvidenceConstructionError,
    PriceEvidenceError,
    PriceFactError,
    PriceFutureObservationError,
    PriceObservationError,
    PriceObservationIdentityError,
    PriceProvenanceMismatchError,
    PriceSourceError,
    PriceSourceIdentityError,
    PriceSourceRejectedError,
    PriceStaleObservationError,
    PriceTargetMismatchError,
    PriceTimestampError,
    RejectingPriceObservationSource,
    produce_price_freshness_evidence,
)

_EXPECTED_ALL = [
    "PriceObservationRequest",
    "VerifiedPriceObservation",
    "PriceObservationSource",
    "RejectingPriceObservationSource",
    "produce_price_freshness_evidence",
    "PriceEvidenceError",
    "PriceArgumentError",
    "PriceSourceError",
    "PriceSourceRejectedError",
    "PriceSourceIdentityError",
    "PriceObservationError",
    "PriceObservationIdentityError",
    "PriceCoverageError",
    "PriceFactError",
    "PriceProvenanceMismatchError",
    "PriceTargetMismatchError",
    "PriceTimestampError",
    "PriceFutureObservationError",
    "PriceStaleObservationError",
    "PriceEvidenceConstructionError",
]


def test_dunder_all_is_exactly_the_frozen_surface() -> None:
    assert marketdata.__all__ == _EXPECTED_ALL


def test_package_public_names_are_exactly_the_frozen_surface() -> None:
    public = {
        name
        for name in dir(marketdata)
        if not name.startswith("_") and not inspect.ismodule(getattr(marketdata, name))
    }
    assert public == set(_EXPECTED_ALL)


def test_module_level_defined_public_names_stay_inside_the_surface() -> None:
    package_dir = Path(marketdata.__file__).resolve().parent
    allowed = set(_EXPECTED_ALL)
    for path in sorted(package_dir.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        import ast

        tree = ast.parse(source, filename=str(path))
        defined: set[str] = set()
        for node in tree.body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.add(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for target in targets:
                    if isinstance(target, ast.Name):
                        defined.add(target.id)
        public_defined = {name for name in defined if not name.startswith("_")}
        assert public_defined <= allowed, (path.name, public_defined - allowed)


def test_error_hierarchy_matches_the_frozen_taxonomy() -> None:
    assert issubclass(PriceEvidenceError, ValueError)
    for cls in (
        PriceArgumentError,
        PriceSourceError,
        PriceSourceIdentityError,
        PriceObservationError,
        PriceProvenanceMismatchError,
        PriceTargetMismatchError,
        PriceTimestampError,
        PriceEvidenceConstructionError,
    ):
        assert cls.__bases__ == (PriceEvidenceError,), cls
    assert PriceSourceRejectedError.__bases__ == (PriceSourceError,)
    assert PriceObservationIdentityError.__bases__ == (PriceObservationError,)
    assert PriceCoverageError.__bases__ == (PriceObservationError,)
    assert PriceFactError.__bases__ == (PriceObservationError,)
    assert PriceFutureObservationError.__bases__ == (PriceTimestampError,)
    assert PriceStaleObservationError.__bases__ == (PriceTimestampError,)


def test_producer_signature_is_the_frozen_boundary() -> None:
    signature = inspect.signature(produce_price_freshness_evidence)
    assert list(signature.parameters) == [
        "provenance",
        "target",
        "dispatch_timestamp",
        "source",
    ]
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY
        for parameter in signature.parameters.values()
    )
    default = signature.parameters["source"].default
    assert isinstance(default, RejectingPriceObservationSource)
    assert default.source_id == "rejecting-price-observation-source-v1"


def test_rejecting_source_identity_and_fail_closed_observe() -> None:
    source = RejectingPriceObservationSource()
    assert source.source_id == "rejecting-price-observation-source-v1"
    with pytest.raises(PriceSourceRejectedError):
        source.observe(None)  # type: ignore[arg-type]
