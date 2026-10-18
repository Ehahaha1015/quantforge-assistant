import json

import pytest

from quantforge.autolab.audit import AuditStore


def test_audit_rejects_path_traversal(tmp_path) -> None:
    with pytest.raises(ValueError):
        AuditStore(tmp_path).create_run("../escape", "run", {})


def test_audit_redacts_transport_secrets(tmp_path) -> None:
    store = AuditStore(tmp_path)
    store.write(
        tmp_path / "result.json", {"stderr": "Authorization: Bearer SECRET-TOKEN-123"}
    )
    content = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert "SECRET-TOKEN-123" not in content["stderr"]
    store.write(
        tmp_path / "result2.json", {"stderr": "Authorization: Token SECRET-TOKEN-456"}
    )
    assert "SECRET-TOKEN-456" not in (
        tmp_path / "result2.json"
    ).read_text(encoding="utf-8")


def test_audit_redacts_secret_values_by_key(tmp_path) -> None:
    store = AuditStore(tmp_path)
    store.write(
        tmp_path / "result.json", {"token": "SECRET", "authorization": "Basic payload"}
    )
    content = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert content == {"authorization": "[REDACTED]", "token": "[REDACTED]"}


def test_audit_redacts_hyphenated_secret_keys(tmp_path) -> None:
    store = AuditStore(tmp_path)
    store.write(tmp_path / "result.json", {"api-key": "LEAK", "x-api-key": "LEAK2"})
    content = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert content == {"api-key": "[REDACTED]", "x-api-key": "[REDACTED]"}


def test_audit_redacts_json_encoded_and_prefixed_secrets(tmp_path) -> None:
    store = AuditStore(tmp_path)
    store.write(
        tmp_path / "result.json",
        {"stdout": '{"token":"SECRET-LEAK"} AKIAIOSFODNN7EXAMPLE'},
    )
    content = (tmp_path / "result.json").read_text(encoding="utf-8")
    assert "SECRET-LEAK" not in content
    assert "AKIAIOSFODNN7EXAMPLE" not in content


def test_audit_redacts_compound_environment_secret_assignment(tmp_path) -> None:
    store = AuditStore(tmp_path)
    store.write(
        tmp_path / "result.json", {"stderr": "AWS_SECRET_ACCESS_KEY=real-secret-value"}
    )
    content = (tmp_path / "result.json").read_text(encoding="utf-8")
    assert "real-secret-value" not in content


def test_audit_write_rejects_external_target(tmp_path) -> None:
    store = AuditStore(tmp_path / "audit")
    with pytest.raises(ValueError):
        store.write(tmp_path / "escaped.json", {})
