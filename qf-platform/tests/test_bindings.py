from __future__ import annotations

import concurrent.futures
import json
import sqlite3
from pathlib import Path

import pytest

from qf_platform.bindings import BindingRegistry
from qf_platform.secrets import WindowsProtector
from qf_platform.storage import PlatformError


def test_explicit_allowance_preserves_fees_spend_secret_and_transport_health(
    tmp_path: Path,
) -> None:
    registry = BindingRegistry(tmp_path)
    profile = registry.save(
        {
            **metadata(),
            "budget_calls": 1,
            "budget_tokens": 4096,
            "budget_cost": "0.01",
            "input_price_per_million": "0.3",
            "output_price_per_million": "1.2",
        },
        {"api_key": "allowance-test-credential"},
    )
    provider = registry.model_provider(profile["id"])
    lease = registry._reserve_model(profile, 100, 100)
    registry._finish_model(profile, lease, True, 1)
    usage = registry.model_usage()[0]
    with registry.connect() as db:
        encrypted = db.execute(
            "SELECT secret FROM profiles WHERE id=?", (profile["id"],)
        ).fetchone()[0]
    updated = registry.extend_model_allowance(
        profile["id"],
        calls=3,
        tokens=8000,
        expected_revision=profile["updated_at"],
        authorization_hash="a" * 64,
    )
    after = registry.model_usage()[0]
    assert after["spent_calls"] == usage["spent_calls"] == 1
    assert after["reserved_tokens"] == usage["reserved_tokens"] == 200
    assert after["reserved_cost"] == usage["reserved_cost"]
    assert after["cost_remaining"] == usage["cost_remaining"]
    assert after["calls_remaining"] == 2
    assert updated["budget_cost"] == profile["budget_cost"]
    assert (
        updated["health"] == profile["health"]
        and updated["last_test"] == profile["last_test"]
    )
    with registry.connect() as db:
        assert (
            db.execute(
                "SELECT secret FROM profiles WHERE id=?", (profile["id"],)
            ).fetchone()[0]
            == encrypted
        )
    with pytest.raises(ValueError, match="REVOKED"):
        provider.check_context({})
    assert registry.audit()[-1]["action"] == "MODEL_ALLOWANCE_AUTHORIZED"


def test_allowance_cannot_lower_caps_change_revision_or_extend_inflight_request(
    tmp_path: Path,
) -> None:
    registry = BindingRegistry(tmp_path)
    profile = registry.save(metadata(), {"api_key": "allowance-fixture-key"})
    options = {
        "calls": 17,
        "tokens": 64000,
        "expected_revision": profile["updated_at"],
        "authorization_hash": "a" * 64,
    }
    with pytest.raises(PlatformError, match="CANNOT_RESET"):
        registry.extend_model_allowance(profile["id"], **{**options, "calls": 1})
    with pytest.raises(PlatformError, match="REVOKED"):
        registry.extend_model_allowance(
            profile["id"], **{**options, "expected_revision": "stale"}
        )
    with pytest.raises(PlatformError, match="INVALID"):
        registry.extend_model_allowance(
            profile["id"], **{**options, "tokens": 1_000_001}
        )
    registry._reserve_model(profile, 100, 100)
    with pytest.raises(PlatformError, match="QUIESCENT"):
        registry.extend_model_allowance(profile["id"], **options)


def metadata() -> dict[str, object]:
    return {
        "kind": "MODEL_PROVIDER",
        "name": "Same vendor",
        "protocol": "OPENAI_COMPATIBLE",
        "base_url": "https://api.example.com/v1",
        "default_model": "research-test",
    }


def test_saved_key_cannot_be_retargeted_without_fresh_secret(tmp_path: Path) -> None:
    registry = BindingRegistry(tmp_path)
    profile = registry.save(
        metadata(),
        {
            "api_key": "offline-original-fixture",
            "custom_headers": {"X-Tenant": "old-private-tenant"},
        },
    )
    for change in (
        {"base_url": "https://attacker.example/v1"},
        {"protocol": "ANTHROPIC_NATIVE"},
    ):
        with pytest.raises(PlatformError, match="ENDPOINT_CHANGE_REQUIRES_NEW_SECRET"):
            registry.save({**metadata(), **change}, {}, profile["id"])
        assert registry.get(profile["id"])["updated_at"] == profile["updated_at"]
    registry.save(
        {**metadata(), "base_url": "https://second.example/v1"},
        {"api_key": "offline-replacement-fixture"},
        profile["id"],
    )
    assert registry._secrets(profile["id"], "MODEL_PROVIDER")["custom_headers"] == {}


def test_os_protection_rotation_deletion_and_no_plaintext(tmp_path: Path) -> None:
    registry = BindingRegistry(tmp_path)
    key = "test-fixture-credential-NEVER-REAL-345678"
    profile = registry.save(
        metadata(),
        {"api_key": key, "custom_headers": {"X-Tenant": "private-tenant-fixture"}},
    )
    assert key not in json.dumps(profile)
    assert key not in json.dumps(registry.list())
    assert key not in json.dumps(registry.audit())
    assert registry._secrets(profile["id"], "MODEL_PROVIDER")["api_key"] == key
    for path in tmp_path.glob("bindings.sqlite*"):
        assert key.encode() not in path.read_bytes()
        assert b"private-tenant-fixture" not in path.read_bytes()
    second = registry.save(metadata(), {"api_key": "second-credential-fixture"})
    assert second["id"] != profile["id"]
    rotated = registry.save(
        metadata(), {"api_key": "rotated-credential-fixture"}, profile["id"]
    )
    assert rotated["secret_identity"] != profile["secret_identity"]
    assert (
        registry._secrets(profile["id"], "MODEL_PROVIDER")["api_key"]
        == "rotated-credential-fixture"
    )
    registry.assign("critic", [{"profile_id": profile["id"], "model": "research-test"}])
    registry.delete(profile["id"])
    with pytest.raises(PlatformError, match="NOT_FOUND"):
        registry.model_provider(profile["id"])
    with pytest.raises(PlatformError, match="NOT_FOUND"):
        registry._secrets(profile["id"], "MODEL_PROVIDER")
    assert len(registry.list()) == 1


def test_domain_separation_disabled_routes_and_audit_immutability(
    tmp_path: Path,
) -> None:
    registry = BindingRegistry(tmp_path)
    profile = registry.save(metadata(), {"api_key": "credential-fixture"})
    with pytest.raises(PlatformError, match="WRONG_DOMAIN"):
        registry._secrets(profile["id"], "BROKER_PROVIDER")
    with pytest.raises(PlatformError, match="INVALID"):
        registry.save(metadata(), {"private_key": "broker-key-fixture"})
    disabled = registry.save({**metadata(), "enabled": False}, {}, profile["id"])
    assert disabled["health"] == "UNTESTED"
    with pytest.raises(PlatformError, match="UNAVAILABLE"):
        registry.assign(
            "director", [{"profile_id": profile["id"], "model": "research-test"}]
        )
    with registry.connect() as db:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("DELETE FROM audit")


@pytest.mark.parametrize(
    "base_url",
    [
        "http://api.example.com",
        "https://secret@api.example.com",
        "https://api.example.com?key=secret",
        "https://127.0.0.1",
        "https://169.254.169.254",
        "https://localhost",
        "https://api.example.com:8443",
    ],
)
def test_credential_urls_and_nonpublic_endpoints_denied(
    tmp_path: Path, base_url: str
) -> None:
    registry = BindingRegistry(tmp_path)
    with pytest.raises(PlatformError, match="INVALID"):
        registry.save(
            {**metadata(), "base_url": base_url}, {"api_key": "credential-fixture"}
        )
    assert registry.list() == []


def test_atomic_independent_profile_saves(tmp_path: Path) -> None:
    registry = BindingRegistry(tmp_path)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        profiles = list(
            pool.map(
                lambda _: registry.save(
                    metadata(), {"api_key": "concurrent-fixture-key"}
                ),
                range(8),
            )
        )
    assert len({p["id"] for p in profiles}) == 8
    assert len(registry.audit()) == 8


def test_dpapi_wrong_ciphertext_fails_closed() -> None:
    with pytest.raises(PlatformError, match="DENIED"):
        WindowsProtector().unprotect(b"invalid-dpapi-ciphertext")


def test_profile_budget_is_global_across_rooms_and_restarts(tmp_path: Path) -> None:
    registry = BindingRegistry(tmp_path)
    profile = registry.save(
        {**metadata(), "budget_calls": 2, "budget_tokens": 20000},
        {"api_key": "quota-fixture"},
    )
    request = registry._reserve_model(profile, 2000, 1800)
    registry._finish_model(profile, request, True, 2)
    restarted = BindingRegistry(tmp_path)
    request = restarted._reserve_model(profile, 2000, 1800)
    restarted._finish_model(profile, request, False, 2)
    with pytest.raises(PlatformError, match="GLOBAL_BUDGET"):
        registry._reserve_model(profile, 1, 1)
    assert sum(v["action"] == "PROFILE_MODEL_REQUEST" for v in restarted.audit()) == 2


def test_crashed_request_retains_concurrency_and_rotation_revokes_old_provider(
    tmp_path: Path,
) -> None:
    registry = BindingRegistry(tmp_path)
    profile = registry.save(metadata(), {"api_key": "quota-fixture"})
    registry._reserve_model(profile, 1, 1)
    with pytest.raises(PlatformError, match="GLOBAL_CONCURRENCY"):
        BindingRegistry(tmp_path)._reserve_model(profile, 1, 1)
    rotated = registry.save(
        metadata(), {"api_key": "rotated-quota-fixture"}, profile["id"]
    )
    with pytest.raises(PlatformError, match="REVISION_REVOKED"):
        registry._reserve_model(profile, 1, 1)
    with pytest.raises(PlatformError, match="GLOBAL_CONCURRENCY"):
        registry._reserve_model(rotated, 1, 1)


def test_director_fallback_is_configured_and_duplicates_are_denied(
    tmp_path: Path,
) -> None:
    registry = BindingRegistry(tmp_path)
    profiles = [
        registry.save(metadata(), {"api_key": "role-fixture"}) for _ in range(2)
    ]
    routes = [{"profile_id": p["id"], "model": "research-test"} for p in profiles]
    registry.assign("director", routes)
    assert registry.roles()["director"] == routes
    with pytest.raises(PlatformError, match="DUPLICATE"):
        registry.assign("validator", [routes[0], routes[0]])


def test_global_capacity_preflight_has_no_room_or_http_reservation(
    tmp_path: Path,
) -> None:
    from decimal import Decimal

    from qf_lab.gateway import GatewayError, ResearchDecision
    from qf_lab.room_gateway import RoomGateway

    registry = BindingRegistry(tmp_path)
    profile = registry.save(metadata(), {"api_key": "quota-fixture"})
    provider = registry.model_provider(profile["id"])
    lease = registry._reserve_model(profile, 1, 1)
    with pytest.raises(GatewayError, match="MODEL_PROFILE_BUSY"):
        provider.check_capacity(100, 100)
    root = tmp_path / "room-budget"
    root.mkdir()
    gateway = RoomGateway(root, calls=8, tokens=64000, cost=Decimal(1))
    with pytest.raises(GatewayError, match="MODEL_PROFILE_BUSY"):
        gateway.call("researcher", provider, {"phase": "first"}, ResearchDecision)
    assert gateway.budget()["spent_calls"] == 0
    registry._finish_model(profile, lease, False, 1)
    provider.check_capacity(100, 100)
    assert registry.model_usage()[0]["spent_calls"] == 1
