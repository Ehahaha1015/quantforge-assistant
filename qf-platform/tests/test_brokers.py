from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from qf_platform.bindings import BindingRegistry
from qf_platform.brokers import CAPABILITIES, BrokerService
from qf_platform.storage import PlatformError, digest


class FixtureGateway:
    adapter, version = "offline-fixture", "1"
    official_documentation = "https://fixture.example/official-api"
    corrupt = False
    expired = False

    def identity(self, material: dict[str, Any]) -> dict[str, Any]:
        return {
            "observed_at": (
                datetime.now(UTC) - timedelta(seconds=60 if self.expired else 0)
            ).isoformat(),
            "provider": "fixturebroker",
            "environment": "PAPER",
            "authenticated": not self.corrupt,
            "account_identity_hash": digest(
                {
                    "account": material["account_reference"],
                    "broker": "fixturebroker",
                    "environment": "PAPER",
                }
            ),
            "response_hash": "a" * 64,
        }

    def account(self, material: dict[str, Any]) -> dict[str, Any]:
        return {
            **self.identity(material),
            "settled_cash": "100.00",
            "available_cash": "90.00",
            "positions": [],
            "open_orders": [
                {
                    "reference": "fixtureorder",
                    "symbol": "FIXTURE",
                    "side": "BUY",
                    "quantity": "100",
                    "price": "0.50",
                    "status": "OPEN",
                    "raw_account": material["account_reference"],
                }
            ],
            "fills": [],
            "server_time": datetime.now(UTC).isoformat(),
            "trading_permission": True,
            "coherent": True,
            "fee_schedule": None,
            "credentials": material["credentials"],
        }

    def capabilities(self, material: dict[str, Any]) -> dict[str, Any]:
        return {
            **self.identity(material),
            "capabilities": {
                k: {"status": "VERIFIED", "evidence_hash": "b" * 64}
                for k in CAPABILITIES
            },
        }

    def quote(self, symbol: str, material: dict[str, Any]) -> dict[str, Any]:
        return {
            **self.identity(material),
            "symbol": symbol,
            "bid": "0.500",
            "ask": "0.501",
            "tick": "0.001",
            "lot_size": 100,
            "session": "CLOSED",
        }

    def query(self, reference: str, material: dict[str, Any]) -> dict[str, Any]:
        return {
            **self.identity(material),
            "order": {
                "reference": reference,
                "symbol": "FIXTURE",
                "side": "BUY",
                "quantity": "100",
                "price": "0.5",
                "status": "UNKNOWN",
            },
        }

    def cancel(self, reference: str, material: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("No cancel transport is authorized")


def fixture(
    tmp_path: Path,
) -> tuple[BindingRegistry, dict[str, Any], FixtureGateway, BrokerService]:
    registry = BindingRegistry(tmp_path)
    meta = registry.save(
        {
            "kind": "BROKER_PROVIDER",
            "name": "Paper fixture",
            "enabled": True,
            "broker_id": "fixturebroker",
            "environment": "PAPER",
            "adapter": "offline-fixture",
            "adapter_version": "1",
        },
        {
            "account_reference": "account-must-never-echo",
            "credentials": {"api_key": "fixture-secret"},
        },
    )
    gateway = FixtureGateway()
    return (
        registry,
        meta,
        gateway,
        BrokerService(tmp_path, registry, {"offline-fixture": lambda: gateway}),
    )


def test_authenticated_observations_masking_query_kill_and_restart(
    tmp_path: Path,
) -> None:
    registry, meta, _, service = fixture(tmp_path)
    state = service.refresh(meta["id"])
    assert state["accounts"][0]["status"] == "AUTHENTICATED_FRESH"
    assert state["accounts"][0]["mode"] == "PAPER"
    assert state["accounts"][0]["observation"]["open_orders"][0]["status"] == "OPEN"
    assert "account-must-never-echo" not in str(state)
    assert "fixture-secret" not in service.ledger.path.read_bytes().decode(
        errors="ignore"
    )
    service.quote(meta["id"], "FIXTURE")
    service.query(meta["id"], "fixtureorder")
    with pytest.raises(PlatformError, match="CANCEL_AUTHORITY"):
        service.operate(
            "cancel", {"profile_id": meta["id"], "order_reference": "fixtureorder"}
        )
    killed = service.kill()
    restarted = BrokerService(tmp_path, registry)
    assert restarted.snapshot()["ledger_hash"] == killed["ledger_hash"]
    assert restarted.snapshot()["operator_enabled"] is False


def test_failed_refresh_stale_observation_and_rotation_never_keep_fresh_badge(
    tmp_path: Path,
) -> None:
    registry, meta, gateway, service = fixture(tmp_path)
    service.refresh(meta["id"])
    gateway.corrupt = True
    with pytest.raises(PlatformError):
        service.refresh(meta["id"])
    assert service.snapshot()["accounts"][0]["status"] == "STALE_OR_DISABLED"
    failure = service.ledger.read()[-1]
    assert (
        failure["classification"] == "BROKER_AUTHENTICATION_TARGET_OR_FRESHNESS_INVALID"
    )
    gateway.corrupt, gateway.expired = False, True
    with pytest.raises(PlatformError):
        service.refresh(meta["id"])
    gateway.expired = False
    service.refresh(meta["id"])
    registry.save(
        {
            "kind": "BROKER_PROVIDER",
            "name": "Rotated paper",
            "enabled": True,
            "broker_id": "fixturebroker",
            "environment": "PAPER",
            "adapter": "offline-fixture",
            "adapter_version": "1",
        },
        {"credentials": {"api_key": "replacement-secret"}},
        meta["id"],
    )
    assert service.snapshot()["accounts"][0]["status"] == "STALE_OR_DISABLED"


def test_vendor_runtime_failure_is_bounded_without_secret_error_echo(
    tmp_path: Path,
) -> None:
    _, meta, gateway, service = fixture(tmp_path)

    def unsafe(material: Any) -> Any:
        raise RuntimeError("fixture-secret raw-account-must-never-echo")

    gateway.account = unsafe  # type: ignore[method-assign]
    with pytest.raises(PlatformError) as failure:
        service.refresh(meta["id"])
    assert "fixture-secret" not in str(failure.value)
    assert "raw-account" not in str(service.ledger.read())
    assert (
        service.ledger.read()[-1]["classification"] == "BROKER_OBSERVATION_UNAVAILABLE"
    )


def test_one_official_plugin_supports_distinct_read_and_execution_queries(
    tmp_path: Path,
) -> None:
    from qf_platform.accounting_book import CoreAccountingBook

    registry, meta, gateway, service = fixture(tmp_path)
    registry.save(
        {
            "kind": "BROKER_PROVIDER",
            "name": "Paused trading plugin fixture",
            "broker_id": "fixturebroker",
            "environment": "PAPER",
            "adapter": "offline-fixture",
            "adapter_version": "1",
            "permission": "TRADING",
            "enabled": True,
        },
        {"credentials": {"core_credential_reference": "offline-reference"}},
        meta["id"],
    )
    gateway.observe = lambda envelope, target, lease: None
    gateway.submit = lambda envelope, observation, lease: None
    gateway.query_attempt = lambda sid, target, generation, epoch, lease: None
    gateway.cancel_order = lambda sid, target, lease: None
    gateway.cancel_capability = lambda target, lease: None
    accounting = CoreAccountingBook(tmp_path / "independent-accounting")
    session = service.execution_session(meta["id"], accounting)
    try:
        from quantforge.frontier import ExecutionTargetBinding

        target = ExecutionTargetBinding(
            schema="frontier-target-v1",
            submission_id="broker1-" + "a" * 32,
            broker="fixturebroker",
            environment="PAPER",
            account_reference="account-"
            + digest({"account": "account-must-never-echo"}),
            adapter_id="offline-fixture",
            adapter_version="1",
        )
        with pytest.raises(PlatformError, match="PAUSED"):
            service.submit(None)  # type: ignore[arg-type]
        with pytest.raises(PlatformError, match="PAUSED"):
            session.dispatch(None, target, None)  # type: ignore[arg-type]
        with pytest.raises(PlatformError, match="PAUSED"):
            session.cancel("offline-order", target)
        # The read query still has its original two-argument contract.
        service.query(meta["id"], "fixtureorder")
        gateway.query_attempt = lambda sid: None
        with pytest.raises(PlatformError, match="SIGNATURE_INVALID"):
            service.execution_session(meta["id"], accounting)
    finally:
        session.close()
