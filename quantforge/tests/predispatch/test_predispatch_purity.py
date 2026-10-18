from __future__ import annotations

import decimal
import inspect
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import quantforge.predispatch as predispatch_package
from quantforge.broker import create_broker_submission_envelope
from quantforge.domain import OrderIntent
from quantforge.live import authorize_live_order, create_live_authorization
from quantforge.portfolio import create_portfolio
from quantforge.predispatch import (
    AuthorizationFreshnessPolicy,
    PreDispatchReason,
    SubmissionHistoryState,
    evaluate_pre_dispatch,
)
from quantforge.risk import RiskLimits, evaluate_order

PREDISPATCH_SOURCE = (
    Path(__file__).resolve().parents[2] / "src" / "quantforge" / "predispatch"
)

ORDER_TIMESTAMP = datetime.fromisoformat("2026-01-02T14:30:00+00:00")
AUTHORIZED_AT = datetime.fromisoformat("2030-01-01T12:00:00+00:00")
EVALUATION_AT = datetime.fromisoformat("2030-01-01T12:05:00+00:00")
OPERATOR = "operator@example.com"
SUBMISSION_ID = "broker1-4c78154fcce6c1791cb74257a777de95"
MAX_AGE = timedelta(minutes=5)

FORBIDDEN_TOKENS = (
    "random",
    "uuid",
    "datetime.now",
    "utcnow",
    "time.time",
    "sleep(",
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
    "Fill",
    "apply_fill",
    "create_portfolio",
    "value_portfolio",
    "execute_paper_order",
    "BrokerSubmissionOutcome",
    "quantforge.execution",
    "quantforge.autolab",
    "place_order",
    "submit_order",
    "cancel_order",
    "replace_order",
    "api_key",
    "secret",
    "token",
    "http://",
    "https://",
)

FORBIDDEN_PUBLIC_NAME = re.compile(r"retry|resubmit|resend|reconcile|submit|cancel|replace")


def limits() -> RiskLimits:
    return RiskLimits(
        max_order_notional=Decimal(1000),
        max_position_notional=Decimal(1000),
        max_gross_exposure=Decimal(1000),
    )


def buy_order() -> OrderIntent:
    return OrderIntent(
        strategy_id="ma",
        symbol="AAPL",
        timestamp=ORDER_TIMESTAMP,
        side="BUY",
        quantity=Decimal(5),
    )


def fixture() -> dict[str, object]:
    order = buy_order()
    permission = authorize_live_order(
        order,
        evaluate_order(
            order,
            create_portfolio(1000),
            {"AAPL": Decimal(100)},
            Decimal(1),
            limits(),
        ),
        create_live_authorization(order, OPERATOR, AUTHORIZED_AT),
    )
    return {
        "envelope": create_broker_submission_envelope(order, permission),
        "history": SubmissionHistoryState(
            submission_id=SUBMISSION_ID, state="NEVER_DISPATCHED"
        ),
        "policy": AuthorizationFreshnessPolicy(max_authorization_age=MAX_AGE),
        "moment": EVALUATION_AT,
        "portfolio": create_portfolio(1000),
        "prices": {"AAPL": Decimal(100)},
        "fee": Decimal(1),
        "limits": limits(),
    }


def run_with(facts: dict[str, object]):
    return evaluate_pre_dispatch(
        facts["envelope"],
        facts["history"],
        facts["policy"],
        facts["moment"],
        facts["portfolio"],
        facts["prices"],
        facts["fee"],
        facts["limits"],
    )


def production_sources() -> list[Path]:
    return sorted(PREDISPATCH_SOURCE.rglob("*.py"))


def test_production_sources_exist() -> None:
    sources = production_sources()
    assert PREDISPATCH_SOURCE / "__init__.py" in sources
    assert PREDISPATCH_SOURCE / "gate.py" in sources


@pytest.mark.parametrize("token", FORBIDDEN_TOKENS)
def test_production_source_avoids_forbidden_token(token: str) -> None:
    for path in production_sources():
        text = path.read_text(encoding="utf-8")
        assert token not in text, f"{token!r} found in {path}"


def test_public_surface_is_exactly_the_contracted_surface() -> None:
    assert set(predispatch_package.__all__) == {
        "PreDispatchError",
        "PreDispatchReason",
        "AuthorizationFreshnessPolicy",
        "SubmissionHistoryState",
        "PreDispatchDecision",
        "evaluate_pre_dispatch",
    }
    assert len(predispatch_package.__all__) == 6
    parameters = list(
        inspect.signature(predispatch_package.evaluate_pre_dispatch).parameters
    )
    assert parameters == [
        "envelope",
        "history",
        "policy",
        "evaluation_timestamp",
        "portfolio",
        "risk_prices",
        "estimated_fee",
        "limits",
    ]


def test_no_retry_or_execution_surface_exists() -> None:
    names = set(predispatch_package.__all__)
    names |= {name for name in vars(predispatch_package) if not name.startswith("_")}
    for exported in (
        predispatch_package.PreDispatchDecision,
        predispatch_package.SubmissionHistoryState,
        predispatch_package.AuthorizationFreshnessPolicy,
    ):
        names |= {name for name in vars(exported) if not name.startswith("_")}
    for name in sorted(names):
        assert not FORBIDDEN_PUBLIC_NAME.search(name), name

    forbidden = (
        "ExecutionManager",
        "ExecutionService",
        "BrokerClient",
        "BrokerAdapter",
        "RetryManager",
        "RetryPolicy",
        "ReconciliationService",
        "Reconciler",
        "StateRepository",
        "SubmissionRepository",
        "OMS",
        "EMS",
        "Router",
    )
    for name in forbidden:
        assert not hasattr(predispatch_package, name)

    for attribute in ("submit", "execute", "place_order", "cancel_order", "replace_order"):
        assert not hasattr(predispatch_package.PreDispatchDecision, attribute)


def test_module_namespace_holds_no_forbidden_capability() -> None:
    names = {
        name for name in vars(predispatch_package.gate) if not name.startswith("_")
    }
    forbidden = {
        "Fill",
        "apply_fill",
        "create_portfolio",
        "value_portfolio",
        "execute_paper_order",
        "BrokerSubmissionOutcome",
    }
    assert not names & forbidden


def test_import_pulls_in_no_execution_or_autolab_package() -> None:
    code = (
        "import sys\n"
        "import quantforge.predispatch\n"
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


def test_module_documents_the_toctou_limitation() -> None:
    docstring = predispatch_package.gate.__doc__ or ""
    assert docstring.strip()
    assert "atomic" in docstring
    assert "first dispatch" in docstring


def context_signature(context: decimal.Context) -> tuple[object, ...]:
    return (
        context.prec,
        context.rounding,
        context.Emin,
        context.Emax,
        context.clamp,
        tuple(
            (str(signal), trapped)
            for signal, trapped in sorted(
                context.traps.items(), key=lambda item: str(item[0])
            )
        ),
    )


def test_hostile_decimal_context_does_not_change_the_decision() -> None:
    expected = run_with(fixture())
    before = context_signature(decimal.getcontext())
    with decimal.localcontext() as hostile:
        hostile.prec = 2
        hostile.rounding = decimal.ROUND_UP
        hostile.Emin = -5
        hostile.Emax = 5
        for signal in (
            decimal.Inexact,
            decimal.Rounded,
            decimal.Underflow,
            decimal.Overflow,
        ):
            hostile.traps[signal] = True
        under_hostile = run_with(fixture())
    assert under_hostile.eligible is True
    assert under_hostile.reasons == ()
    assert under_hostile == expected
    assert under_hostile.model_dump_json() == expected.model_dump_json()
    assert context_signature(decimal.getcontext()) == before


def test_inputs_are_immutable_after_success_and_failure() -> None:
    facts = fixture()
    snapshot = {
        "envelope": facts["envelope"].model_dump(),
        "history": facts["history"].model_dump(),
        "policy": facts["policy"].model_dump(),
        "portfolio": facts["portfolio"].model_dump(),
        "limits": facts["limits"].model_dump(),
    }
    run_with(facts)
    assert facts["envelope"].model_dump() == snapshot["envelope"]
    assert facts["history"].model_dump() == snapshot["history"]
    assert facts["policy"].model_dump() == snapshot["policy"]
    assert facts["portfolio"].model_dump() == snapshot["portfolio"]
    assert facts["limits"].model_dump() == snapshot["limits"]

    facts["portfolio"] = create_portfolio(100)
    denied = run_with(facts)
    assert denied.reasons == (PreDispatchReason.FRESH_RISK_REJECTED,)
    assert facts["envelope"].model_dump() == snapshot["envelope"]
    assert facts["history"].model_dump() == snapshot["history"]


FRESH_PROCESS_SNIPPET = (
    "from datetime import datetime, timedelta\n"
    "from decimal import Decimal\n"
    "from quantforge.broker import create_broker_submission_envelope\n"
    "from quantforge.domain import OrderIntent\n"
    "from quantforge.live import authorize_live_order, create_live_authorization\n"
    "from quantforge.portfolio import create_portfolio\n"
    "from quantforge.predispatch import (\n"
    "    AuthorizationFreshnessPolicy, SubmissionHistoryState,"
    " evaluate_pre_dispatch,\n"
    ")\n"
    "from quantforge.risk import RiskLimits, evaluate_order\n"
    "order = OrderIntent(strategy_id='ma', symbol='AAPL',\n"
    "    timestamp=datetime.fromisoformat('2026-01-02T14:30:00+00:00'),\n"
    "    side='BUY', quantity=Decimal('5'))\n"
    "limits = RiskLimits(max_order_notional=Decimal(1000),\n"
    "    max_position_notional=Decimal(1000), max_gross_exposure=Decimal(1000))\n"
    "permission = authorize_live_order(order,\n"
    "    evaluate_order(order, create_portfolio(1000), {'AAPL': Decimal(100)},\n"
    "        Decimal(1), limits),\n"
    "    create_live_authorization(order, 'operator@example.com',\n"
    "        datetime.fromisoformat('2030-01-01T12:00:00+00:00')))\n"
    "decision = evaluate_pre_dispatch(\n"
    "    create_broker_submission_envelope(order, permission),\n"
    "    SubmissionHistoryState(\n"
    "        submission_id='broker1-4c78154fcce6c1791cb74257a777de95',\n"
    "        state='NEVER_DISPATCHED'),\n"
    "    AuthorizationFreshnessPolicy(\n"
    "        max_authorization_age=timedelta(minutes=5)),\n"
    "    datetime.fromisoformat('2030-01-01T12:05:00+00:00'),\n"
    "    create_portfolio(1000), {'AAPL': Decimal(100)}, Decimal(1), limits)\n"
    "print(decision.eligible)\n"
    "print('|'.join(reason.value for reason in decision.reasons))\n"
)


@pytest.mark.parametrize("hash_seed", ["random", "0", "12345"])
def test_decision_is_reproducible_in_a_fresh_interpreter(hash_seed: str) -> None:
    environment = dict(os.environ)
    environment["PYTHONHASHSEED"] = hash_seed
    completed = subprocess.run(
        [sys.executable, "-c", FRESH_PROCESS_SNIPPET],
        capture_output=True,
        text=True,
        check=True,
        env=environment,
    )
    assert completed.stdout.splitlines() == ["True", ""]


MULTI_REASON_SNIPPET = (
    "from datetime import datetime, timedelta\n"
    "from decimal import Decimal\n"
    "from quantforge.broker import create_broker_submission_envelope\n"
    "from quantforge.domain import OrderIntent\n"
    "from quantforge.live import authorize_live_order, create_live_authorization\n"
    "from quantforge.portfolio import create_portfolio\n"
    "from quantforge.predispatch import (\n"
    "    AuthorizationFreshnessPolicy, SubmissionHistoryState,"
    " evaluate_pre_dispatch,\n"
    ")\n"
    "from quantforge.risk import RiskLimits, evaluate_order\n"
    "order = OrderIntent(strategy_id='ma', symbol='AAPL',\n"
    "    timestamp=datetime.fromisoformat('2026-01-02T14:30:00+00:00'),\n"
    "    side='BUY', quantity=Decimal('5'))\n"
    "limits = RiskLimits(max_order_notional=Decimal(1000),\n"
    "    max_position_notional=Decimal(1000), max_gross_exposure=Decimal(1000))\n"
    "permission = authorize_live_order(order,\n"
    "    evaluate_order(order, create_portfolio(1000), {'AAPL': Decimal(100)},\n"
    "        Decimal(1), limits),\n"
    "    create_live_authorization(order, 'operator@example.com',\n"
    "        datetime.fromisoformat('2030-01-01T12:00:00+00:00')))\n"
    "decision = evaluate_pre_dispatch(\n"
    "    create_broker_submission_envelope(order, permission),\n"
    "    SubmissionHistoryState(\n"
    "        submission_id='broker1-4c78154fcce6c1791cb74257a777de95',\n"
    "        state='OUTCOME_UNKNOWN'),\n"
    "    AuthorizationFreshnessPolicy(\n"
    "        max_authorization_age=timedelta(minutes=5)),\n"
    "    datetime.fromisoformat('2030-01-01T12:06:00+00:00'),\n"
    "    create_portfolio(100), {'AAPL': Decimal(100)}, Decimal(1), limits)\n"
    "print(decision.eligible)\n"
    "print('|'.join(reason.value for reason in decision.reasons))\n"
)


def test_multi_reason_output_is_stable_across_hash_seeds() -> None:
    outputs = set()
    for hash_seed in ("random", "0", "12345"):
        environment = dict(os.environ)
        environment["PYTHONHASHSEED"] = hash_seed
        completed = subprocess.run(
            [sys.executable, "-c", MULTI_REASON_SNIPPET],
            capture_output=True,
            text=True,
            check=True,
            env=environment,
        )
        outputs.add(completed.stdout)
    assert outputs == {
        "False\nSTALE_AUTHORIZATION|FRESH_RISK_REJECTED|SUBMISSION_OUTCOME_UNKNOWN\n"
    }
