from __future__ import annotations

import json
from pathlib import Path

import pytest

from quantforge.experiments import canonical_json
from quantforge.product.scale.storage import ScaleError
from quantforge.product.scale.vault import HostBenchmarkCapability, inspect_seal, seal


def test_encrypted_artifact_authentication_no_plaintext_or_key_in_projection(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    key = tmp_path / "private" / "program.key"
    payload = {
        "benchmark": {"secret_sentinel": "FINAL_PRICE_91723.451"},
        "rows": [1, 2, 3],
    }
    expected = seal(
        root,
        "program",
        payload,
        {"universe_id": "fixed", "partition_id": "2024"},
        key_path=key,
    )
    raw = (root / "benchmark.sealed.json").read_bytes()
    assert b"FINAL_PRICE_91723.451" not in raw
    assert key.read_bytes() not in raw
    assert inspect_seal(root) == expected
    capability = HostBenchmarkCapability("program", key)
    assert capability.decrypt(root, expected=expected) == payload
    wrong = tmp_path / "private" / "wrong.key"
    wrong.write_bytes(b"x" * 32)
    with pytest.raises(ScaleError, match="AUTHENTICATION"):
        HostBenchmarkCapability("program", wrong).decrypt(root, expected=expected)
    changed = json.loads(raw)
    changed["binding"]["partition_id"] = "changed"
    (root / "benchmark.sealed.json").write_text(canonical_json(changed))
    with pytest.raises(ScaleError, match="BINDING"):
        capability.decrypt(root, expected=expected)


def test_key_cannot_be_in_assurance(tmp_path: Path) -> None:
    path = tmp_path / ".autolab"
    path.mkdir()
    with pytest.raises(ScaleError, match="OUTSIDE"):
        seal(path, "prog", {}, {}, key_path=path / "bad.key")


@pytest.mark.parametrize("component", [".autolab", ".AUTOLAB", ".AutoLab"])
def test_assurance_case_variant_rejected_before_key_creation(tmp_path: Path, component: str) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    key = tmp_path / component / "bad.key"
    with pytest.raises(ScaleError, match="OUTSIDE"):
        seal(root, "prog", {}, {}, key_path=key)
    assert not key.exists()
    key.parent.mkdir()
    key.write_bytes(b"x" * 32)
    with pytest.raises(ScaleError, match="INVALID_HOST_KEY_PATH"):
        HostBenchmarkCapability("prog", key)
