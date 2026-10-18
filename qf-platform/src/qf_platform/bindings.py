"""Domain-separated binding metadata and protected secrets, never browser echoes."""

from __future__ import annotations

import builtins
import hashlib
import json
import re
import sqlite3
import threading
import uuid
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .secrets import Protector, WindowsProtector
from .storage import PlatformError, canonical, digest


class ModelProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["MODEL_PROVIDER"]
    name: str = Field(min_length=1, max_length=80)
    enabled: bool = True
    protocol: Literal["OPENAI_COMPATIBLE", "ANTHROPIC_NATIVE", "GEMINI_NATIVE"]
    base_url: str = Field(max_length=500)
    default_model: str = Field(min_length=1, max_length=120)
    models: list[str] = Field(default_factory=list, max_length=100)
    priority: int = Field(default=0, ge=0, le=100)
    timeout: int = Field(default=60, ge=1, le=120)
    max_concurrency: int = Field(default=1, ge=1, le=8)
    budget_calls: int = Field(default=16, ge=1, le=100)
    budget_tokens: int = Field(default=64000, ge=4096, le=1000000)
    budget_cost: str = "1.00"
    input_price_per_million: str = "0.00"
    output_price_per_million: str = "0.00"
    cost_currency: Literal["USD", "CNY"] = "USD"
    generation_mode: Literal["DEFAULT", "NON_THINKING"] = "DEFAULT"

    @field_validator("base_url")
    @classmethod
    def endpoint(cls, value: str) -> str:
        from qf_lab.protocols import validate_endpoint

        return validate_endpoint(value)

    @field_validator("default_model", "models")
    @classmethod
    def model_ids(cls, value: str | list[str]) -> str | list[str]:
        items = [value] if isinstance(value, str) else value
        if any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,119}", v) for v in items
        ):
            raise ValueError("MODEL_ID_INVALID")
        if len(items) != len(set(items)):
            raise ValueError("DUPLICATE_MODEL_ID")
        return value

    @field_validator(
        "budget_cost", "input_price_per_million", "output_price_per_million"
    )
    @classmethod
    def money(cls, value: str) -> str:
        if not re.fullmatch(r"(?:0|[1-9][0-9]{0,6})(?:\.[0-9]{1,8})?", value):
            raise ValueError("PLAIN_NONNEGATIVE_DECIMAL_REQUIRED")
        return value


class BrokerProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["BROKER_PROVIDER"]
    name: str = Field(min_length=1, max_length=80)
    enabled: bool = False
    broker_id: str = Field(min_length=1, max_length=80)
    environment: Literal["PRODUCTION", "PAPER"]
    adapter: str = Field(min_length=1, max_length=80)
    adapter_version: str = Field(min_length=1, max_length=40)
    base_url: str = Field(default="", max_length=500)
    permission: Literal["READ_ONLY", "TRADING"] = "READ_ONLY"

    @field_validator("base_url")
    @classmethod
    def endpoint(cls, value: str) -> str:
        if not value:
            return value  # Local vendor SDKs need no HTTP endpoint.
        from qf_lab.protocols import validate_endpoint

        return validate_endpoint(value)


SECRET_FIELDS = {
    "MODEL_PROVIDER": {"api_key", "custom_headers"},
    "BROKER_PROVIDER": {"account_reference", "credentials", "certificates"},
}
ROLE_NAMES = {"researcher", "critic", "director", "validator"}


class BindingRegistry:
    def __init__(self, root: Path, protector: Protector | None = None) -> None:
        from quantforge.product.storage import local_root

        self.root = local_root(root)
        self.path = self.root / "bindings.sqlite"
        self.protector = protector if protector is not None else WindowsProtector()
        # SQLite creates a WAL database's -wal/-shm files on first use and
        # deletes them when the last connection closes, so threads opening at
        # the same instant race to create them and all but one fail with
        # SQLITE_READONLY. Serialise connection creation. See
        # docs/WAL-CONNECTION-LIFETIME-20261016.md.
        self._connect_lock = threading.Lock()
        with closing(self.connect()) as db, db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute(
                "CREATE TABLE IF NOT EXISTS profiles (id TEXT PRIMARY KEY, "
                "metadata TEXT NOT NULL, secret BLOB NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS audit "
                "(seq INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS roles "
                "(role TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
            for op in ("UPDATE", "DELETE"):
                db.execute(
                    f"CREATE TRIGGER IF NOT EXISTS audit_no_{op.lower()} BEFORE {op} "
                    "ON audit BEGIN SELECT RAISE(ABORT,'append-only'); END"
                )

    def connect(self) -> sqlite3.Connection:
        from quantforge.product.storage import local_root

        local_root(self.root)
        if self.path.is_symlink():
            raise PlatformError("REDIRECTED_BINDING_STORE")
        # sqlite3.connect() is lazy: the first statement creates the
        # shared-memory files, so the pragma stays inside the lock.
        with self._connect_lock:
            db = sqlite3.connect(self.path, timeout=10)
            db.execute("PRAGMA synchronous=FULL")
        return db

    @staticmethod
    def _audit(
        db: sqlite3.Connection, action: str, profile_id: str, **values: Any
    ) -> None:
        db.execute(
            "INSERT INTO audit(payload) VALUES(?)",
            (
                canonical(
                    {
                        "action": action,
                        "profile_id": profile_id,
                        "at": datetime.now(UTC).isoformat(),
                        **values,
                    }
                ),
            ),
        )

    def save(
        self,
        metadata: dict[str, Any],
        secrets: dict[str, Any],
        profile_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            cls = cast(
                type[BaseModel],
                {
                    "MODEL_PROVIDER": ModelProfile,
                    "BROKER_PROVIDER": BrokerProfile,
                }[metadata["kind"]],
            )
            profile: dict[str, Any] = cls.model_validate(metadata).model_dump(
                mode="json"
            )
            if set(secrets) - SECRET_FIELDS[profile["kind"]]:
                raise ValueError
            if len(canonical(secrets).encode()) > 30000:
                raise ValueError
            if profile_id is not None and not re.fullmatch(
                r"binding-[a-f0-9]{32}", profile_id
            ):
                raise ValueError
        except (KeyError, ValueError, TypeError):
            raise PlatformError("BINDING_CONFIGURATION_INVALID") from None
        identity = profile_id or "binding-" + uuid.uuid4().hex
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT metadata,secret FROM profiles WHERE id=?", (identity,)
            ).fetchone()
            if profile_id is not None and old is None:
                raise PlatformError("BINDING_NOT_FOUND")
            prior = json.loads(old[0]) if old else {}
            if prior and prior["kind"] != profile["kind"]:
                raise PlatformError("BINDING_DOMAIN_IMMUTABLE")
            if (
                prior
                and profile["kind"] == "MODEL_PROVIDER"
                and (
                    prior["base_url"] != profile["base_url"]
                    or prior["protocol"] != profile["protocol"]
                )
            ):
                # A saved credential is pinned to its destination/protocol.
                # Endpoint replacement requires freshly supplied material.
                if not secrets.get("api_key"):
                    raise PlatformError("MODEL_ENDPOINT_CHANGE_REQUIRES_NEW_SECRET")
                if "custom_headers" not in secrets:
                    secrets = {**secrets, "custom_headers": {}}
            protected: dict[str, Any] = (
                json.loads(self.protector.unprotect(old[1])) if old else {}
            )
            protected.update(secrets)
            if profile["kind"] == "MODEL_PROVIDER":
                key = protected.get("api_key")
                if (
                    not isinstance(key, str)
                    or not 8 <= len(key) <= 4096
                    or any(c in key for c in "\r\n")
                ):
                    raise PlatformError("MODEL_KEY_REQUIRED")
                headers = protected.get("custom_headers", {})
                if (
                    not isinstance(headers, dict)
                    or len(headers) > 12
                    or any(
                        not isinstance(k, str)
                        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,59}", k)
                        or k.lower()
                        in {
                            "host",
                            "authorization",
                            "content-length",
                            "transfer-encoding",
                            "content-type",
                            "x-api-key",
                        }
                        or not isinstance(v, str)
                        or len(v) > 2048
                        or any(c in v for c in "\r\n")
                        for k, v in headers.items()
                    )
                ):
                    raise PlatformError("MODEL_HEADERS_INVALID")
            elif profile["kind"] == "BROKER_PROVIDER":
                if profile["enabled"] and (
                    not isinstance(protected.get("account_reference"), str)
                    or not 1 <= len(protected["account_reference"]) <= 128
                    or not isinstance(protected.get("credentials"), dict)
                ):
                    raise PlatformError("BROKER_ACCOUNT_AND_CREDENTIALS_REQUIRED")
            stamp = datetime.now(UTC).isoformat()
            account = protected.get("account_reference")
            profile.update(
                {
                    "id": identity,
                    "created_at": prior.get("created_at", stamp),
                    "updated_at": stamp,
                    "secret_identity": "protected:"
                    + hashlib.sha256(canonical(protected).encode()).hexdigest()[:12],
                    "masked_account": "account:"
                    + hashlib.sha256(str(account).encode()).hexdigest()[:12]
                    if account
                    else None,
                    "health": "UNTESTED",
                    "mode": "UNBOUND",
                    "last_test": None,
                }
            )
            encrypted = self.protector.protect(canonical(protected).encode())
            db.execute(
                "INSERT INTO profiles VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "metadata=excluded.metadata,secret=excluded.secret",
                (identity, canonical(profile), encrypted),
            )
            self._audit(
                db,
                "CREATE" if old is None else "UPDATE",
                identity,
                metadata_hash=digest(profile),
            )
            db.commit()
        return profile

    def delete(self, profile_id: str) -> None:
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute("DELETE FROM profiles WHERE id=?", (profile_id,))
            if cursor.rowcount != 1:
                raise PlatformError("BINDING_NOT_FOUND")
            # Access revoked; historical SQLite pages contain only DPAPI ciphertext.
            self._audit(db, "DELETE", profile_id)
            db.commit()

    def extend_model_allowance(
        self,
        profile_id: str,
        *,
        calls: int,
        tokens: int,
        expected_revision: str,
        authorization_hash: str,
    ) -> dict[str, Any]:
        """Host-only explicit quota grant; never reset usage, fees or credentials."""
        if (
            type(calls) is not int
            or not 1 <= calls <= 100
            or type(tokens) is not int
            or not 4096 <= tokens <= 1_000_000
            or not re.fullmatch(r"[a-f0-9]{64}", authorization_hash)
        ):
            raise PlatformError("MODEL_ALLOWANCE_INVALID")
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT metadata FROM profiles WHERE id=?", (profile_id,)
            ).fetchone()
            if row is None:
                raise PlatformError("BINDING_NOT_FOUND")
            current = cast(dict[str, Any], json.loads(row[0]))
            if current["kind"] != "MODEL_PROVIDER" or not current["enabled"]:
                raise PlatformError("MODEL_BINDING_UNAVAILABLE")
            if current["updated_at"] != expected_revision:
                raise PlatformError("MODEL_BINDING_REVISION_REVOKED")
            if calls < current["budget_calls"] or tokens < current["budget_tokens"]:
                raise PlatformError("MODEL_ALLOWANCE_CANNOT_RESET")
            history = [
                json.loads(value[0])
                for value in db.execute("SELECT payload FROM audit")
            ]
            requests = [
                value
                for value in history
                if value["action"] == "PROFILE_MODEL_REQUEST"
                and value["profile_id"] == profile_id
            ]
            completed = {
                value["lease"]
                for value in history
                if value["action"] == "PROFILE_MODEL_RESULT"
            }
            if any(value["lease"] not in completed for value in requests):
                raise PlatformError("MODEL_ALLOWANCE_REQUIRES_QUIESCENT_PROFILE")
            if calls == current["budget_calls"] and tokens == current["budget_tokens"]:
                return current
            before = {
                key: current[key]
                for key in ("budget_calls", "budget_tokens", "budget_cost")
            }
            current.update(
                budget_calls=calls,
                budget_tokens=tokens,
                updated_at=datetime.now(UTC).isoformat(),
            )
            # Quota-only changes preserve transport health and protected bytes.
            db.execute(
                "UPDATE profiles SET metadata=? WHERE id=?",
                (canonical(current), profile_id),
            )
            self._audit(
                db,
                "MODEL_ALLOWANCE_AUTHORIZED",
                profile_id,
                authorization_hash=authorization_hash,
                before=before,
                after={key: current[key] for key in before},
                usage="LIFETIME_USAGE_RETAINED",
                spend="EXISTING_FEE_CAP_UNCHANGED",
            )
            db.commit()
        return current

    def list(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as db:
            return [
                json.loads(row[0])
                for row in db.execute("SELECT metadata FROM profiles ORDER BY id")
                if json.loads(row[0])["kind"] in SECRET_FIELDS
            ]

    def get(self, profile_id: str) -> dict[str, Any]:
        for item in self.list():
            if item["id"] == profile_id:
                return item
        raise PlatformError("BINDING_NOT_FOUND")

    def _secrets(
        self, profile_id: str, domain: str, revision: str | None = None
    ) -> dict[str, Any]:
        with closing(self.connect()) as db:
            row = db.execute(
                "SELECT metadata,secret FROM profiles WHERE id=?", (profile_id,)
            ).fetchone()
            if row is None:
                raise PlatformError("BINDING_NOT_FOUND")
            meta = json.loads(row[0])
            if meta["kind"] != domain or meta["enabled"] is not True:
                raise PlatformError("BINDING_DISABLED_OR_WRONG_DOMAIN")
            if revision is not None and meta["updated_at"] != revision:
                raise PlatformError("BINDING_REVISION_REVOKED")
            return dict(json.loads(self.protector.unprotect(row[1])))

    def model_provider(self, profile_id: str, model: str | None = None) -> Any:
        from qf_lab.gateway import GatewayError
        from qf_lab.protocols import ProtocolProvider

        meta = self.get(profile_id)
        if meta["kind"] != "MODEL_PROVIDER" or not meta["enabled"]:
            raise PlatformError("MODEL_BINDING_UNAVAILABLE")
        selected = model or meta["default_model"]
        if selected not in [meta["default_model"], *meta["models"]]:
            raise PlatformError("MODEL_NOT_ALLOWED")

        def reserve(input_bound: int, output_bound: int) -> str:
            try:
                return self._reserve_model(meta, input_bound, output_bound)
            except PlatformError as exc:
                code = {
                    "PROFILE_GLOBAL_CONCURRENCY_EXHAUSTED": (
                        "MODEL_GLOBAL_CAPACITY_DENIED"
                    ),
                    "PROFILE_GLOBAL_BUDGET_EXHAUSTED": "MODEL_PROFILE_BUDGET_EXHAUSTED",
                    "MODEL_BINDING_REVISION_REVOKED": "MODEL_BINDING_REVISION_REVOKED",
                }.get(str(exc), "MODEL_REQUEST_DENIED")
                raise GatewayError(code) from None

        def capacity(input_bound: int, output_bound: int) -> None:
            try:
                self._secrets(profile_id, "MODEL_PROVIDER", meta["updated_at"])
            except PlatformError:
                raise GatewayError("MODEL_BINDING_REVISION_REVOKED") from None
            usage = next(u for u in self.model_usage() if u["profile_id"] == profile_id)
            if usage["in_flight_or_unresolved"] >= meta["max_concurrency"]:
                raise GatewayError("MODEL_PROFILE_BUSY")
            cost = (
                Decimal(input_bound) * Decimal(meta["input_price_per_million"])
                + Decimal(output_bound) * Decimal(meta["output_price_per_million"])
            ) / 1_000_000
            if (
                usage["calls_remaining"] < 1
                or usage["tokens_remaining"] < input_bound + output_bound
                or Decimal(usage["cost_remaining"]) < cost
            ):
                raise GatewayError("MODEL_PROFILE_BUDGET_EXHAUSTED")

        return ProtocolProvider(
            profile_id,
            meta["protocol"],
            meta["base_url"],
            selected,
            secret=lambda: self._secrets(
                profile_id, "MODEL_PROVIDER", meta["updated_at"]
            ),
            timeout=meta["timeout"],
            input_price=Decimal(meta["input_price_per_million"]),
            output_price=Decimal(meta["output_price_per_million"]),
            limits={
                "calls": meta["budget_calls"],
                "tokens": meta["budget_tokens"],
                "cost": meta["budget_cost"],
            },
            max_concurrency=meta["max_concurrency"],
            binding_revision=meta["updated_at"],
            cost_currency=meta["cost_currency"],
            generation_mode=meta["generation_mode"],
            before_request=reserve,
            capacity_check=capacity,
            after_request=lambda lease, success, latency: self._finish_model(
                meta, lease, success, latency
            ),
        )

    def _reserve_model(
        self, meta: dict[str, Any], input_bound: int, output_bound: int
    ) -> str:
        lease = "request-" + uuid.uuid4().hex
        reserved_tokens = input_bound + output_bound
        cost = (
            Decimal(input_bound) * Decimal(meta["input_price_per_million"])
            + Decimal(output_bound) * Decimal(meta["output_price_per_million"])
        ) / 1_000_000
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT metadata FROM profiles WHERE id=?", (meta["id"],)
            ).fetchone()
            if (
                current is None
                or json.loads(current[0])["updated_at"] != meta["updated_at"]
                or json.loads(current[0])["enabled"] is not True
            ):
                raise PlatformError("MODEL_BINDING_REVISION_REVOKED")
            rows = [json.loads(v[0]) for v in db.execute("SELECT payload FROM audit")]
            requests = [
                v
                for v in rows
                if v["action"] == "PROFILE_MODEL_REQUEST"
                and v["profile_id"] == meta["id"]
            ]
            completed = {
                v["lease"] for v in rows if v["action"] == "PROFILE_MODEL_RESULT"
            }
            if (
                sum(v["lease"] not in completed for v in requests)
                >= meta["max_concurrency"]
            ):
                raise PlatformError("PROFILE_GLOBAL_CONCURRENCY_EXHAUSTED")
            if (
                len(requests) >= meta["budget_calls"]
                or sum(v["reserved_tokens"] for v in requests) + reserved_tokens
                > meta["budget_tokens"]
                or sum((Decimal(v["reserved_cost"]) for v in requests), Decimal(0))
                + cost
                > Decimal(meta["budget_cost"])
            ):
                raise PlatformError("PROFILE_GLOBAL_BUDGET_EXHAUSTED")
            self._audit(
                db,
                "PROFILE_MODEL_REQUEST",
                meta["id"],
                lease=lease,
                revision=meta["updated_at"],
                reserved_tokens=reserved_tokens,
                reserved_cost=str(cost),
                pricing="DECLARED_CONSERVATIVE_RESERVATION",
                retry="FORBIDDEN",
            )
            db.commit()
        return lease

    def _finish_model(
        self, meta: dict[str, Any], lease: str, success: bool, latency: int
    ) -> None:
        with closing(self.connect()) as db, db:
            self._audit(
                db,
                "PROFILE_MODEL_RESULT",
                meta["id"],
                lease=lease,
                status="HTTP_VERIFIED_RESPONSE" if success else "FAILED_OR_UNKNOWN",
                latency_ms=latency,
                spend="CONSERVATIVE_RESERVATION_RETAINED",
            )

    def test(self, profile_id: str, *, discover: bool = False) -> dict[str, Any]:
        from qf_lab.gateway import GatewayError

        meta = self.get(profile_id)
        if meta["kind"] != "MODEL_PROVIDER":
            # Brokers require their own authenticated evidence adapters.
            raise PlatformError("DOMAIN_PROBE_REQUIRED")
        # Commit intent before the potentially chargeable request. A crashed test
        # stays visible; no client or backend automatically retries it.
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            attempts = sum(
                json.loads(row[0]).get("action") == "TEST_REQUEST"
                and json.loads(row[0]).get("profile_id") == profile_id
                for row in db.execute("SELECT payload FROM audit")
            )
            if attempts >= meta["budget_calls"]:
                raise PlatformError("CONNECTION_TEST_BUDGET_EXHAUSTED")
            self._audit(db, "TEST_REQUEST", profile_id, discover=discover)
            db.commit()
        result: dict[str, Any]
        try:
            provider = self.model_provider(profile_id)
            models = provider.discover() if discover else provider.test()
            result = {"status": "AUTHENTICATED_MODEL_RESPONSE", "models": models}
        except (GatewayError, PlatformError):
            result = {"status": "CONNECTION_TEST_FAILED", "models": []}
        with closing(self.connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT metadata FROM profiles WHERE id=?", (profile_id,)
            ).fetchone()
            if row is None or json.loads(row[0])["updated_at"] != meta["updated_at"]:
                raise PlatformError("BINDING_CHANGED_DURING_TEST")
            current = cast(dict[str, Any], json.loads(row[0]))
            current["last_test"] = {**result, "at": datetime.now(UTC).isoformat()}
            if discover and result["status"] == "AUTHENTICATED_MODEL_RESPONSE":
                current["models"] = sorted(
                    set(current["models"]) | set(result["models"])
                )[:100]
            current["health"] = (
                "HEALTHY"
                if result["status"] == "AUTHENTICATED_MODEL_RESPONSE"
                else "FAILED"
            )
            # A model test does not bind a broker domain.
            current["mode"] = "REAL" if current["health"] == "HEALTHY" else "UNBOUND"
            db.execute(
                "UPDATE profiles SET metadata=? WHERE id=?",
                (canonical(current), profile_id),
            )
            self._audit(
                db,
                "MODEL_DISCOVERY" if discover else "CONNECTION_TEST",
                profile_id,
                status=result["status"],
            )
            db.commit()
        return current

    def assign(self, role: str, routes: builtins.list[dict[str, str]]) -> None:
        if role not in ROLE_NAMES or not 1 <= len(routes) <= 4:
            raise PlatformError("ROLE_ROUTING_INVALID")
        if len({(v.get("profile_id"), v.get("model")) for v in routes}) != len(routes):
            raise PlatformError("DUPLICATE_MODEL_ROUTE")
        for route in routes:
            if set(route) != {"profile_id", "model"}:
                raise PlatformError("ROLE_ROUTING_INVALID")
            self.model_provider(route["profile_id"], route["model"])
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT INTO roles VALUES(?,?) ON CONFLICT(role) "
                "DO UPDATE SET payload=excluded.payload",
                (role, canonical(routes)),
            )
            self._audit(db, "ROLE_ASSIGNMENT", role, routes=routes)

    def roles(self) -> dict[str, Any]:
        with closing(self.connect()) as db:
            return {
                role: json.loads(payload)
                for role, payload in db.execute(
                    "SELECT role,payload FROM roles ORDER BY role"
                )
            }

    def model_usage(self) -> builtins.list[dict[str, Any]]:
        """Profile-lifetime conservative reservations, including failed calls."""
        with closing(self.connect()) as db:
            rows = [
                json.loads(row[0]) for row in db.execute("SELECT payload FROM audit")
            ]
        completed = {v["lease"] for v in rows if v["action"] == "PROFILE_MODEL_RESULT"}
        result = []
        for profile in self.list():
            if profile["kind"] != "MODEL_PROVIDER":
                continue
            requests = [
                v
                for v in rows
                if v["action"] == "PROFILE_MODEL_REQUEST"
                and v["profile_id"] == profile["id"]
            ]
            tokens = sum(v["reserved_tokens"] for v in requests)
            cost = sum((Decimal(v["reserved_cost"]) for v in requests), Decimal(0))
            result.append(
                {
                    "profile_id": profile["id"],
                    "spent_calls": len(requests),
                    "calls_remaining": max(0, profile["budget_calls"] - len(requests)),
                    "reserved_tokens": tokens,
                    "tokens_remaining": max(0, profile["budget_tokens"] - tokens),
                    "reserved_cost": str(cost),
                    "cost_remaining": str(
                        max(Decimal(0), Decimal(profile["budget_cost"]) - cost)
                    ),
                    "cost_currency": profile["cost_currency"],
                    "in_flight_or_unresolved": sum(
                        v["lease"] not in completed for v in requests
                    ),
                    "accounting": (
                        "PROFILE_LIFETIME_CONSERVATIVE_RESERVATION_NOT_PROVIDER_BILL"
                    ),
                }
            )
        return result

    def audit(self, after: int = 0, limit: int = 100) -> builtins.list[dict[str, Any]]:
        if not 0 <= after or not 1 <= limit <= 100:
            raise PlatformError("HISTORY_PAGE_INVALID")
        with closing(self.connect()) as db:
            return [
                {"seq": seq, **json.loads(payload)}
                for seq, payload in db.execute(
                    "SELECT seq,payload FROM audit WHERE seq>? ORDER BY seq LIMIT ?",
                    (after, limit),
                )
            ]
