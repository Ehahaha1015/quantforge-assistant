from __future__ import annotations

import inspect
import re
import subprocess
import sys
from pathlib import Path

import pytest

import quantforge.broker as broker_package

BROKER_SOURCE = Path(__file__).resolve().parents[2] / "src" / "quantforge" / "broker"

# Coarse guards required by the installed contract. The real guarantee is the
# value design, the exception split, and the absence of any transport or retry
# surface; a token scan alone would prove less than it claims.
FORBIDDEN_TOKENS = (
    "random",
    "uuid",
    "datetime.now",
    "utcnow",
    "time.time",
    "socket",
    "requests",
    "httpx",
    "aiohttp",
    "websocket",
    "urllib",
    "subprocess",
    "os.environ",
    "open(",
    "hash(",
    "model_construct",
    "tenacity",
    "backoff",
    "max_retries",
    "sleep(",
    "Fill",
    "apply_fill",
    "create_portfolio",
    "value_portfolio",
    "PortfolioState",
    "evaluate_order",
    "execute_paper_order",
    "quantforge.execution",
    "quantforge.portfolio",
    "paper1-",
    "live1-",
    "place_order",
    "cancel_order",
    "replace_order",
    "api_key",
    "secret",
    "token",
    "http://",
    "https://",
)

FORBIDDEN_PUBLIC_NAME = re.compile(r"retry|resubmit|resend|attempt|reconcile")

FORBIDDEN_ATTRIBUTES = (
    "submit",
    "execute",
    "place_order",
    "cancel_order",
    "replace_order",
    "reconcile",
)


def production_sources() -> list[Path]:
    return sorted(BROKER_SOURCE.rglob("*.py"))


def test_production_sources_exist() -> None:
    sources = production_sources()
    assert BROKER_SOURCE / "__init__.py" in sources
    assert BROKER_SOURCE / "submission.py" in sources


@pytest.mark.parametrize("token", FORBIDDEN_TOKENS)
def test_production_source_avoids_forbidden_token(token: str) -> None:
    for path in production_sources():
        text = path.read_text(encoding="utf-8")
        assert token not in text, f"{token!r} found in {path}"


def test_public_names_have_no_retry_or_reconciliation_surface() -> None:
    names = set(broker_package.__all__)
    names |= {
        name for name in vars(broker_package) if not name.startswith("_")
    }
    for exported in (
        broker_package.BrokerSubmissionEnvelope,
        broker_package.BrokerSubmissionOutcome,
    ):
        names |= {
            name for name in vars(exported) if not name.startswith("_")
        }
    for name in sorted(names):
        assert not FORBIDDEN_PUBLIC_NAME.search(name), name


def test_artifacts_expose_no_effectful_method() -> None:
    for exported in (
        broker_package.BrokerSubmissionEnvelope,
        broker_package.BrokerSubmissionOutcome,
    ):
        for forbidden in FORBIDDEN_ATTRIBUTES:
            assert not hasattr(exported, forbidden)


def test_public_surface_is_exactly_the_contracted_surface() -> None:
    assert set(broker_package.__all__) == {
        "BrokerBoundaryError",
        "BrokerPreDispatchError",
        "BrokerSubmissionEnvelope",
        "BrokerSubmissionOutcome",
        "broker_submission_id",
        "create_broker_submission_envelope",
    }
    assert len(broker_package.__all__) == 6
    assert issubclass(broker_package.BrokerBoundaryError, ValueError)
    assert issubclass(broker_package.BrokerPreDispatchError, ValueError)
    assert list(inspect.signature(broker_package.broker_submission_id).parameters) == [
        "order"
    ]
    assert list(
        inspect.signature(
            broker_package.create_broker_submission_envelope
        ).parameters
    ) == ["order", "permission"]
    forbidden = (
        "BrokerClient",
        "BrokerAdapter",
        "BrokerService",
        "BrokerGateway",
        "BrokerRouter",
        "ExecutionManager",
        "OrderManager",
        "OrderRepository",
        "SubmissionRepository",
        "SubmissionQueue",
        "RetryPolicy",
        "Reconciler",
        "ReconciliationService",
        "MockBroker",
        "FakeExchange",
        "SimulatedLiveBroker",
        "MatchingEngine",
    )
    for name in forbidden:
        assert not hasattr(broker_package, name)


def test_broker_import_pulls_in_no_execution_or_autolab_package() -> None:
    code = (
        "import sys\n"
        "import quantforge.broker\n"
        "leaked = [name for name in ('quantforge.execution', 'quantforge.autolab')"
        " if name in sys.modules]\n"
        "print(','.join(leaked))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert completed.stdout.strip() == ""


def test_module_namespace_holds_no_accounting_or_risk_api() -> None:
    names = {name for name in vars(broker_package.submission) if not name.startswith("_")}
    forbidden = {
        "Fill",
        "apply_fill",
        "create_portfolio",
        "value_portfolio",
        "PortfolioState",
        "evaluate_order",
        "execute_paper_order",
    }
    assert not names & forbidden


def test_production_never_raises_the_adapter_only_error() -> None:
    for path in production_sources():
        text = path.read_text(encoding="utf-8")
        assert "raise BrokerPreDispatchError" not in text, path


def test_documentation_does_not_overclaim_broker_idempotency() -> None:
    for path in production_sources():
        lowered = path.read_text(encoding="utf-8").lower()
        for overclaim in ("exactly-once", "exactly once", "guarantee", "guaranteed"):
            assert overclaim not in lowered, f"{overclaim!r} in {path}"
