"""Capability sentinel probe: PRICE-001 must never touch the fenced surfaces.

Third-party and frozen imports are warmed first (the allowed pydantic
dependency reads its own plugin flag from the environment whenever a model
class is built, which is outside PRICE-001's control), then the sentinels
are installed, then the PRICE-001 package is imported and exercised with a
successful and a failing producer call. Environment access is covered by the
static fence instead: the section 21 import allow-list excludes ``os``,
``getpass`` and every credential module, so the shipped package cannot read
the environment at all.
"""

from __future__ import annotations

import builtins
import random
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import NoReturn

_ROOT = Path(__file__).resolve().parents[2]
for _entry in (str(_ROOT), str(_ROOT / "src"), str(_ROOT / "tests" / "marketdata")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

# Warm the third-party and frozen dependency graph before the sentinels exist.
import pydantic  # noqa: F401

from tests.provenance._fixtures import (
    EVALUATED_AT,
    SUBMISSION_ID,
    make_artifact,
)


def _fire(*args: object, **kwargs: object) -> NoReturn:
    raise AssertionError("capability sentinel fired")


socket.socket = _fire  # type: ignore[assignment]
subprocess.Popen = _fire  # type: ignore[assignment]
builtins.open = _fire  # type: ignore[assignment]
random.random = _fire  # type: ignore[assignment]
random.choice = _fire  # type: ignore[assignment]
time.time = _fire  # type: ignore[assignment]
time.monotonic = _fire  # type: ignore[assignment]

from datetime import timedelta

from quantforge.frontier import ExecutionTargetBinding
from quantforge.marketdata import (
    PriceSourceRejectedError,
    RejectingPriceObservationSource,
    VerifiedPriceObservation,
    produce_price_freshness_evidence,
)
from quantforge.marketdata.canonical import _observation_id

_SOURCE_ID = "marketdata-sentinel-source"
_OBSERVED_AT = EVALUATED_AT - timedelta(seconds=10)
_MAXIMUM_AGE = timedelta(seconds=30)


class _Source:
    def __init__(self, observation: VerifiedPriceObservation) -> None:
        self._observation = observation

    @property
    def source_id(self) -> str:
        return _SOURCE_ID

    def observe(self, request: object) -> VerifiedPriceObservation:
        return self._observation


def _main() -> None:
    provenance = make_artifact()
    target = ExecutionTargetBinding(
        schema="frontier-target-v1",
        broker="broker-x",
        environment="SANDBOX",
        account_reference="acct-7",
        adapter_id="adapter-x",
        adapter_version="1",
        submission_id=SUBMISSION_ID,
    )
    observation = VerifiedPriceObservation(
        schema="marketdata-observation-v1",
        observation_id=_observation_id(
            market_source=_SOURCE_ID,
            source_observation_id="obs-1",
            observed_at=_OBSERVED_AT,
            maximum_age=_MAXIMUM_AGE,
            risk_prices=provenance.risk_prices,
        ),
        market_source=_SOURCE_ID,
        source_observation_id="obs-1",
        observed_at=_OBSERVED_AT,
        maximum_age=_MAXIMUM_AGE,
        risk_prices=provenance.risk_prices,
    )
    evidence = produce_price_freshness_evidence(
        provenance=provenance,
        target=target,
        dispatch_timestamp=EVALUATED_AT,
        source=_Source(observation),
    )
    assert evidence.schema == "frontier-price-v1"
    try:
        produce_price_freshness_evidence(
            provenance=provenance,
            target=target,
            dispatch_timestamp=EVALUATED_AT,
            source=RejectingPriceObservationSource(),
        )
    except PriceSourceRejectedError:
        pass
    else:
        raise AssertionError("rejecting source must not produce evidence")
    print("SENTINELS_CLEAN")


_main()
