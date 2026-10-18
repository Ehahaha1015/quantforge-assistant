"""Official broker extension boundary and disabled production composition.

The user selects the broker. No guessed vendor, scraping transport, synthetic
account or blanket assumption about API/idempotency is installed here.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, cast

from quantforge.broker import BrokerSubmissionEnvelope
from quantforge.execution_ledger import LockedBrokerAdapter

from .accounting_book import CoreAccountingBook
from .bindings import BindingRegistry
from .storage import Ledger, PlatformError, digest

CAPABILITIES = (
    "client_order_id",
    "idempotency",
    "order_query",
    "cancel",
    "partial_fill",
    "unknown_recovery",
    "limit_order",
    "lot_validation",
    "tick_validation",
    "session_validation",
)

OBSERVATION_FAILURE_CODES = {
    "BROKER_AUTHENTICATION_TARGET_OR_FRESHNESS_INVALID",
    "BROKER_QUOTE_TARGET_MISMATCH",
    "BROKER_QUOTE_INVALID",
    "BROKER_ORDER_INVALID",
    "BROKER_ORDER_STATUS_INVALID",
    "BROKER_QUERY_TARGET_MISMATCH",
    "BROKER_ACCOUNT_OBSERVATION_INCOMPLETE",
    "BROKER_ACCOUNT_RECONCILIATION_REQUIRED",
    "BROKER_POSITION_INVALID",
    "BROKER_CAPABILITY_PROBE_INVALID",
    "BROKER_EXACT_DECIMAL_REQUIRED",
    "BROKER_BINDING_DISABLED",
    "BINDING_REVISION_REVOKED",
    "USER_SELECTED_OFFICIAL_BROKER_ADAPTER_REQUIRED",
    "BROKER_ADAPTER_VERSION_OR_DOCUMENTATION_MISMATCH",
}


class OfficialBrokerGateway(Protocol):
    """Implemented only by an explicit broker-supported API/SDK plugin.

    Its documentation/version and authenticated transport proofs are required
    per observation. Secrets are leased to this host backend, never to Lab.
    """

    adapter: str
    version: str
    official_documentation: str

    def account(self, credentials: dict[str, Any]) -> dict[str, Any]: ...

    def quote(self, symbol: str, credentials: dict[str, Any]) -> dict[str, Any]: ...

    def capabilities(self, credentials: dict[str, Any]) -> dict[str, Any]: ...

    def query(
        self, order_reference: str, credentials: dict[str, Any]
    ) -> dict[str, Any]: ...

    def cancel(
        self, order_reference: str, credentials: dict[str, Any]
    ) -> dict[str, Any]: ...


class BrokerService:
    def __init__(
        self,
        root: Path,
        registry: BindingRegistry,
        gateways: dict[str, Callable[[], OfficialBrokerGateway]] | None = None,
    ) -> None:
        directory = root / "broker-observations"
        directory.mkdir(exist_ok=True)
        self.ledger = Ledger(directory)
        self.registry = registry
        self.gateways = gateways or {}
        self.adapter = LockedBrokerAdapter()
        if not self.ledger.read():
            with self.ledger.transaction() as db:
                if not self.ledger.history(db):
                    self.ledger.append(
                        db,
                        kind="OPERATOR_DISABLED",
                        reason="USER_REAL_EXPERIMENT_PAUSED",
                        mode="UNBOUND",
                    )

    def kill(self) -> dict[str, Any]:
        with self.ledger.transaction() as db:
            self.ledger.append(
                db, kind="OPERATOR_DISABLED", reason="USER_KILL_SWITCH", mode="UNBOUND"
            )
        return self.snapshot()

    def operate(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        fields = {
            "kill": set(),
            "refresh": {"profile_id"},
            "quote": {"profile_id", "symbol"},
            "query": {"profile_id", "order_reference"},
            "cancel": {"profile_id", "order_reference"},
        }
        if (
            action not in fields
            or set(payload) != fields[action]
            or any(not isinstance(v, str) for v in payload.values())
        ):
            raise PlatformError("BROKER_OPERATION_INVALID")
        if action == "kill":
            return self.kill()
        if action == "refresh":
            return self.refresh(payload["profile_id"])
        if action == "quote":
            return self.quote(payload["profile_id"], payload["symbol"])
        if action == "cancel":
            raise PlatformError("REAL_EXPERIMENT_PAUSED_CANCEL_AUTHORITY_REQUIRED")
        return self.query(payload["profile_id"], payload["order_reference"])

    def _failure(self, profile_id: str, operation: str, error: Exception) -> None:
        classification = (
            str(error)
            if type(error) is PlatformError and str(error) in OBSERVATION_FAILURE_CODES
            else "BROKER_OBSERVATION_UNAVAILABLE"
        )
        with self.ledger.transaction() as db:
            self.ledger.append(
                db,
                kind="BROKER_OBSERVATION_FAILED",
                profile_id=profile_id
                if re.fullmatch(r"binding-[a-f0-9]{32}", profile_id)
                else None,
                operation=operation,
                status="UNAVAILABLE",
                classification=classification,
                mode="UNBOUND",
            )

    def quote(self, profile_id: str, symbol: str) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,40}", symbol):
            raise PlatformError("BROKER_SYMBOL_INVALID")
        try:
            meta, gateway, material = self._gateway(profile_id)
            response = gateway.quote(symbol, material)
            self._identity(response, meta, material)
            if response["symbol"] != symbol:
                raise PlatformError("BROKER_QUOTE_TARGET_MISMATCH")
            observation = {
                "symbol": symbol,
                "bid": self._decimal(response["bid"]),
                "ask": self._decimal(response["ask"]),
                "tick": self._decimal(response["tick"]),
                "lot_size": response["lot_size"],
                "session": response["session"],
                "observed_at": response["observed_at"],
                "response_hash": response["response_hash"],
            }
            if (
                type(observation["lot_size"]) is not int
                or observation["lot_size"] < 1
                or observation["session"]
                not in {"OPEN", "CLOSED", "SUSPENDED", "UNKNOWN"}
                or Decimal(observation["bid"]) > Decimal(observation["ask"])
                or Decimal(observation["tick"]) <= 0
            ):
                raise PlatformError("BROKER_QUOTE_INVALID")
            self.registry._secrets(profile_id, "BROKER_PROVIDER", meta["updated_at"])
            with self.ledger.transaction() as db:
                self.ledger.append(
                    db,
                    kind="AUTHENTICATED_QUOTE",
                    profile_id=profile_id,
                    revision=meta["updated_at"],
                    quote=observation,
                    mode="REAL" if meta["environment"] == "PRODUCTION" else "PAPER",
                )
        except Exception as error:
            self._failure(profile_id, "quote", error)
            raise PlatformError("BROKER_QUOTE_UNAVAILABLE") from None
        return self.snapshot()

    @classmethod
    def _order(cls, item: dict[str, Any], *, fill: bool = False) -> dict[str, Any]:
        if (
            not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", item["reference"])
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,40}", item["symbol"])
            or item["side"] not in {"BUY", "SELL"}
        ):
            raise PlatformError("BROKER_ORDER_INVALID")
        output = {
            "reference": item["reference"],
            "symbol": item["symbol"],
            "side": item["side"],
            "quantity": cls._decimal(item["quantity"]),
            "price": cls._decimal(item["price"]),
        }
        if fill:
            output["fees"] = cls._decimal(item["fees"])
        else:
            if item["status"] not in {
                "OPEN",
                "PARTIAL",
                "FILLED",
                "CANCELLED",
                "REJECTED",
                "UNKNOWN",
            }:
                raise PlatformError("BROKER_ORDER_STATUS_INVALID")
            output["status"] = item["status"]
        return output

    def query(self, profile_id: str, order_reference: str) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", order_reference):
            raise PlatformError("BROKER_ORDER_REFERENCE_INVALID")
        try:
            meta, gateway, material = self._gateway(profile_id)
            response = gateway.query(order_reference, material)
            self._identity(response, meta, material)
            order = self._order(response["order"])
            if order["reference"] != order_reference:
                raise PlatformError("BROKER_QUERY_TARGET_MISMATCH")
            self.registry._secrets(profile_id, "BROKER_PROVIDER", meta["updated_at"])
            with self.ledger.transaction() as db:
                self.ledger.append(
                    db,
                    kind="AUTHENTICATED_ORDER_QUERY",
                    profile_id=profile_id,
                    revision=meta["updated_at"],
                    order=order,
                    response_hash=response["response_hash"],
                    mode="REAL" if meta["environment"] == "PRODUCTION" else "PAPER",
                )
        except Exception as error:
            self._failure(profile_id, "query", error)
            raise PlatformError("BROKER_QUERY_UNAVAILABLE") from None
        return self.snapshot()

    def submit(self, envelope: BrokerSubmissionEnvelope) -> None:
        # A platform plan/model cannot grant Core HumanLiveAuthorization or
        # Frontier/operator/durable EXECSTATE dispatch authority. Session paused.
        raise PlatformError("REAL_EXPERIMENT_PAUSED_NO_DISPATCH_AUTHORITY")

    def execution_session(
        self,
        profile_id: str,
        accounting: CoreAccountingBook,
    ) -> Any:
        """Backend plugin composition with production/operator authority closed.

        Read-only registry credentials cannot replace Core's scoped Windows
        credential boundary. A selected official execution plugin must implement
        the typed contract and reference a separately provisioned Core vault
        entry. This method provides no live enable or submit HTTP endpoint.
        """
        from datetime import timedelta
        from inspect import signature

        from quantforge.credentials import CredentialReference
        from quantforge.operator import (
            OperatorControlPolicy,
            SQLiteOperatorControlStore,
        )

        from .broker_execution import BrokerExecutionSession, ExecutionGateway

        meta, gateway, material = self._gateway(profile_id)
        if meta["permission"] != "TRADING" or not all(
            callable(getattr(gateway, name, None))
            for name in (
                "observe",
                "submit",
                "query_attempt",
                "cancel_order",
                "cancel_capability",
            )
        ):
            raise PlatformError("OFFICIAL_EXECUTION_CONTRACT_NOT_BOUND")
        for name, arity in {
            "observe": 3,
            "submit": 3,
            "query_attempt": 5,
            "cancel_order": 3,
            "cancel_capability": 2,
        }.items():
            try:
                signature(getattr(gateway, name)).bind(*([None] * arity))
            except (TypeError, ValueError):
                raise PlatformError("OFFICIAL_EXECUTION_SIGNATURE_INVALID") from None
        reference = CredentialReference(
            value=material["credentials"].get("core_credential_reference", "")
        )
        account_ref = "account-" + digest({"account": material["account_reference"]})
        directory = self.registry.root / "broker-execution" / profile_id
        directory.mkdir(parents=True, exist_ok=True)

        def guard(target: Any) -> None:
            self.registry._secrets(profile_id, "BROKER_PROVIDER", meta["updated_at"])
            if (
                target.broker != meta["broker_id"]
                or target.environment != meta["environment"]
                or target.account_reference != account_ref
                or target.adapter_id != meta["adapter"]
                or target.adapter_version != meta["adapter_version"]
            ):
                raise PlatformError("BROKER_EXECUTION_BINDING_TARGET_MISMATCH")

        operator = SQLiteOperatorControlStore(
            directory / "operator.sqlite3",
            policy=OperatorControlPolicy(
                schema="operator-policy-v1",
                maximum_age_upper_bound=timedelta(minutes=5),
            ),
        )  # Frozen rejecting verifier; no fabricated operator enable evidence.
        return BrokerExecutionSession(
            directory,
            cast(ExecutionGateway, gateway),
            operator,
            reference,
            binding_guard=guard,
            paused=lambda: True,
            accounting=accounting,
            owns_operator=True,
        )

    def _gateway(
        self, profile_id: str
    ) -> tuple[dict[str, Any], OfficialBrokerGateway, dict[str, Any]]:
        meta = self.registry.get(profile_id)
        if meta["kind"] != "BROKER_PROVIDER" or not meta["enabled"]:
            raise PlatformError("BROKER_BINDING_DISABLED")
        factory = self.gateways.get(meta["adapter"])
        if factory is None:
            raise PlatformError("USER_SELECTED_OFFICIAL_BROKER_ADAPTER_REQUIRED")
        gateway = factory()
        if (
            gateway.adapter != meta["adapter"]
            or gateway.version != meta["adapter_version"]
            or not gateway.official_documentation.startswith("https://")
        ):
            raise PlatformError("BROKER_ADAPTER_VERSION_OR_DOCUMENTATION_MISMATCH")
        return (
            meta,
            gateway,
            self.registry._secrets(profile_id, "BROKER_PROVIDER", meta["updated_at"]),
        )

    @staticmethod
    def _decimal(value: Any) -> str:
        if not isinstance(value, str) or not re.fullmatch(
            r"(?:0|[1-9][0-9]{0,15})(?:\.[0-9]{1,8})?", value
        ):
            raise PlatformError("BROKER_EXACT_DECIMAL_REQUIRED")
        return str(Decimal(value))

    @staticmethod
    def _identity(
        value: dict[str, Any], meta: dict[str, Any], material: dict[str, Any]
    ) -> None:
        try:
            instant = datetime.fromisoformat(value["observed_at"])
            expected = digest(
                {
                    "account": material["account_reference"],
                    "broker": meta["broker_id"],
                    "environment": meta["environment"],
                }
            )
            if (
                instant.tzinfo is None
                or not 0 <= (datetime.now(UTC) - instant).total_seconds() <= 30
                or value["account_identity_hash"] != expected
                or value["environment"] != meta["environment"]
                or value["provider"] != meta["broker_id"]
                or value["authenticated"] is not True
                or not re.fullmatch(r"[a-f0-9]{64}", value["response_hash"])
            ):
                raise ValueError
        except (KeyError, ValueError, TypeError):
            raise PlatformError(
                "BROKER_AUTHENTICATION_TARGET_OR_FRESHNESS_INVALID"
            ) from None

    def refresh(self, profile_id: str) -> dict[str, Any]:
        try:
            meta, gateway, material = self._gateway(profile_id)
            response = gateway.account(material)
            self._identity(response, meta, material)
            for field in (
                "settled_cash",
                "available_cash",
                "positions",
                "open_orders",
                "fills",
                "server_time",
                "trading_permission",
                "coherent",
                "fee_schedule",
            ):
                if field not in response:
                    raise PlatformError("BROKER_ACCOUNT_OBSERVATION_INCOMPLETE")
            if (
                response["coherent"] is not True
                or type(response["trading_permission"]) is not bool
                or not isinstance(response["positions"], list)
                or not isinstance(response["open_orders"], list)
                or not isinstance(response["fills"], list)
            ):
                raise PlatformError("BROKER_ACCOUNT_RECONCILIATION_REQUIRED")
            positions = []
            for item in response["positions"]:
                if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,40}", item["symbol"]):
                    raise PlatformError("BROKER_POSITION_INVALID")
                positions.append(
                    {
                        "symbol": item["symbol"],
                        "quantity": self._decimal(item["quantity"]),
                        "market_value": self._decimal(item["market_value"]),
                    }
                )
            # Never expose arbitrary SDK payload/account/credential fields.
            observation = {
                "profile_id": profile_id,
                "provider": meta["broker_id"],
                "environment": meta["environment"],
                "masked_account": meta["masked_account"],
                "observed_at": response["observed_at"],
                "cash": self._decimal(response["settled_cash"]),
                "available_cash": self._decimal(response["available_cash"]),
                "positions": positions,
                "open_order_count": len(response["open_orders"]),
                "fill_count": len(response["fills"]),
                "trading_permission": response["trading_permission"],
                "fee_schedule_available": response["fee_schedule"] is not None,
                "coherent": True,
                "response_hash": response["response_hash"],
                "revision": meta["updated_at"],
                "open_orders": [self._order(v) for v in response["open_orders"]],
                "fills": [self._order(v, fill=True) for v in response["fills"]],
                "mode": "REAL" if meta["environment"] == "PRODUCTION" else "PAPER",
                "official_documentation": gateway.official_documentation,
            }
            probe = gateway.capabilities(material)
            self._identity(probe, meta, material)
            capabilities = probe["capabilities"]
            if set(capabilities) != set(CAPABILITIES) or any(
                not isinstance(v, dict)
                or v.get("status") not in {"VERIFIED", "UNKNOWN", "UNSUPPORTED"}
                or (
                    v["status"] == "VERIFIED"
                    and not re.fullmatch(r"[a-f0-9]{64}", v.get("evidence_hash", ""))
                )
                for v in capabilities.values()
            ):
                raise PlatformError("BROKER_CAPABILITY_PROBE_INVALID")
            self.registry._secrets(profile_id, "BROKER_PROVIDER", meta["updated_at"])
            with self.ledger.transaction() as db:
                self.ledger.append(
                    db,
                    kind="AUTHENTICATED_ACCOUNT_OBSERVATION",
                    **observation,
                    capabilities={k: v["status"] for k, v in capabilities.items()},
                    capability_evidence_hash=probe["response_hash"],
                )
        except Exception as error:
            self._failure(profile_id, "account", error)
            raise PlatformError("BROKER_READ_UNAVAILABLE") from None
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        history = self.ledger.read()
        accounts = []
        for meta in self.registry.list():
            if meta["kind"] != "BROKER_PROVIDER":
                continue
            observations = [
                v
                for v in history
                if v["kind"] == "AUTHENTICATED_ACCOUNT_OBSERVATION"
                and v["profile_id"] == meta["id"]
            ]
            latest = observations[-1] if observations else None
            failed = any(
                v["kind"] == "BROKER_OBSERVATION_FAILED"
                and v["profile_id"] == meta["id"]
                and v["operation"] == "account"
                and (latest is None or v["seq"] > latest["seq"])
                for v in history
            )
            age = (
                (
                    datetime.now(UTC) - datetime.fromisoformat(latest["observed_at"])
                ).total_seconds()
                if latest
                else None
            )
            accounts.append(
                {
                    "profile_id": meta["id"],
                    "name": meta["name"],
                    "masked_account": meta["masked_account"],
                    "mode": latest["mode"] if latest else "UNBOUND",
                    "status": "AUTHENTICATED_FRESH"
                    if latest
                    and age is not None
                    and 0 <= age <= 30
                    and meta["enabled"]
                    and not failed
                    and latest["revision"] == meta["updated_at"]
                    else "STALE_OR_DISABLED"
                    if latest
                    else "OFFICIAL_BROKER_ADAPTER_NOT_BOUND",
                    "observation": latest,
                    "capabilities": latest["capabilities"]
                    if latest
                    else dict.fromkeys(CAPABILITIES, "UNKNOWN"),
                }
            )
        return {
            "schema_version": "qf-brokers-v1",
            "accounts": accounts,
            "operator_enabled": False,
            "live_authority": "PAUSED",
            "kill_switch": "PERSISTENT_DISABLED",
            "observed_at": datetime.now(UTC).isoformat(),
            "ledger_hash": history[-1]["event_hash"],
            "unresolved_live_submission": "NOT_SCANNED_PRODUCTION_AUTHORITY_PAUSED",
            "transport": "CORE_COMPOSITION_PAUSED_OFFICIAL_EXECUTION_PLUGIN_REQUIRED",
            "quotes": [v for v in history if v["kind"] == "AUTHENTICATED_QUOTE"][-20:],
            "order_queries": [
                v for v in history if v["kind"] == "AUTHENTICATED_ORDER_QUERY"
            ][-20:],
        }
