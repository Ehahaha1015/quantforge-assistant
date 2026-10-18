"""Publication interruptions preserve spent evidence and never imply permission."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, Self

import pytest

from quantforge.experiments import ExperimentRecord, ExperimentStore, canonical_json
from quantforge.research import oos

from ._fixtures import ADMITTED_AT, Fixture

_PRIVATE_ERROR = "private injected filesystem details"
_COLLISION_BYTES = b"existing reservation must never be overwritten"
Snapshot = tuple[dict[str, bytes], set[str], tuple[ExperimentRecord, ...]]


@pytest.fixture
def host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Fixture]:
    fixture = Fixture(tmp_path / "h")
    try:
        (fixture.root / "host-owned.bin").write_bytes(b"preserve unrelated host bytes")
        recipe = fixture.spec.recipe.to_payload()
        fixture.store.save(
            ExperimentRecord.create(
                experiment_id="unrelated.host",
                git_commit=recipe["code_context"]["git_commit"],
                dataset_id=recipe["dataset_snapshot"]["dataset_id"],
                dataset_hash=recipe["dataset_snapshot"]["content_hash"],
                backtest_config={"host_owned": True},
                random_seed=0,
                started_at=ADMITTED_AT,
                metrics={"host_owned": True},
                artifact_refs=[],
            )
        )

        def forbid_save(*args: Any, **kwargs: Any) -> None:
            pytest.fail("OOS admission attempted to save an ExperimentRecord")

        monkeypatch.setattr(ExperimentStore, "save", forbid_save)
        yield fixture
    finally:
        fixture.close()


@pytest.fixture
def canonical_seal(tmp_path: Path) -> bytes:
    """Obtain expected bytes through a successful native public admission."""
    reference = Fixture(tmp_path / "r")
    try:
        receipt = reference.seal()
        assert receipt["state"] == "FIRST_USE_SEALED"
        assert receipt["test_use_allowed"] is True
        assert receipt["fresh_within_retained_history"] is True
        return reference.seal_path().read_bytes()
    finally:
        reference.close()


def _snapshot(host: Fixture) -> Snapshot:
    directories = {
        path.relative_to(host.path).as_posix()
        for path in host.path.rglob("*")
        if path.is_dir()
    }
    return host.file_bytes(), directories, host.store.list()


def _assert_evidence(
    host: Fixture,
    before: Snapshot,
    *,
    directory: bool,
    data: bytes | None,
) -> None:
    files, directories, records = before
    path = host.seal_path()
    expected_files = dict(files)
    expected_directories = set(directories)
    if directory:
        expected_directories.add(path.parent.relative_to(host.path).as_posix())
    if data is not None:
        expected_files[path.relative_to(host.path).as_posix()] = data
    assert _snapshot(host) == (expected_files, expected_directories, records)


def _denied(call: Any, **kwargs: Any) -> None:
    with pytest.raises(oos.ResearchSealError) as caught:
        call(**kwargs)
    assert _PRIVATE_ERROR not in str(caught.value)


def _assert_partial_blocks(host: Fixture) -> None:
    before = _snapshot(host)
    host.reopen()
    assert _snapshot(host) == before
    _denied(host.seal)
    assert _snapshot(host) == before
    _denied(host.seal, mode="recovery")
    assert _snapshot(host) == before
    _denied(host.verify)
    assert _snapshot(host) == before


def _assert_exact_recovery(
    host: Fixture,
    canonical_seal: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _snapshot(host)
    host.reopen()
    assert _snapshot(host) == before

    def forbid_publish(*args: Any, **kwargs: Any) -> None:
        pytest.fail("recovery or repeated first use attempted publication")

    with monkeypatch.context() as isolated:
        isolated.setattr(oos, "_publish", forbid_publish)
        _denied(
            host.seal,
            mode="recovery",
            admitted_at=ADMITTED_AT + timedelta(microseconds=1),
        )
        assert _snapshot(host) == before
        receipt = host.seal(mode="recovery")
        assert receipt["state"] == "SAME_EXPERIMENT_RECOVERY"
        assert receipt["test_use_allowed"] is True
        assert receipt["fresh_within_retained_history"] is False
        assert receipt["terminal_outcome"] is None
        assert receipt["seal"] == json.loads(canonical_seal)
        assert host.seal_path().read_bytes() == canonical_seal
        assert _snapshot(host) == before
        verified = host.verify()
        assert verified["state"] == "VERIFIED_NO_TEST_PERMISSION"
        assert verified["test_use_allowed"] is False
        assert verified["fresh_within_retained_history"] is False
        _denied(host.seal)
        assert _snapshot(host) == before


class _FaultingStream:
    def __init__(self, stream: Any, fault: str, hits: list[str]) -> None:
        self.stream = stream
        self.fault = fault
        self.hits = hits
        self.writes = 0

    def __enter__(self) -> Self:
        self.stream.__enter__()
        return self

    def __exit__(self, *args: object) -> Any:
        return self.stream.__exit__(*args)

    def write(self, data: bytes) -> int:
        self.writes += 1
        if self.fault == "write-empty":
            self.hits.append(self.fault)
            raise OSError(_PRIVATE_ERROR)
        if self.fault in {"write-partial", "short-write"}:
            self.hits.append(self.fault)
            if self.writes > 1:
                raise OSError(_PRIVATE_ERROR)
            count = self.stream.write(data[: len(data) // 2])
            if self.fault == "write-partial":
                raise OSError(_PRIVATE_ERROR)
            return count
        return self.stream.write(data)

    def flush(self) -> None:
        if self.fault == "flush":
            self.hits.append(self.fault)
            raise OSError(_PRIVATE_ERROR)
        self.stream.flush()

    def fileno(self) -> int:
        return self.stream.fileno()


@pytest.mark.parametrize(
    "stage",
    [
        "before-mkdir",
        "after-mkdir",
        "mkdir-collision",
        "before-create",
        "after-create",
        "create-collision",
    ],
)
def test_exclusive_reservation_failure_preserves_evidence(
    host: Fixture,
    stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _snapshot(host)
    path = host.seal_path()
    original_mkdir, original_open = Path.mkdir, Path.open
    hits: list[str] = []

    def fault_mkdir(target: Path, *args: Any, **kwargs: Any) -> Any:
        if target == path.parent:
            assert not kwargs.get("exist_ok", False)
            if stage == "before-mkdir":
                hits.append(stage)
                raise OSError(_PRIVATE_ERROR)
            if stage == "mkdir-collision":
                original_mkdir(target)
                with original_open(path, "xb") as stream:
                    stream.write(_COLLISION_BYTES)
                hits.append(stage)
                # Exercise the actual exclusive mkdir against retained evidence.
                return original_mkdir(target, *args, **kwargs)
            result = original_mkdir(target, *args, **kwargs)
            if stage == "after-mkdir":
                hits.append(stage)
                raise OSError(_PRIVATE_ERROR)
            return result
        return original_mkdir(target, *args, **kwargs)

    def fault_open(target: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if target == path and "x" in mode:
            assert mode == "xb"
            if stage == "before-create":
                hits.append(stage)
                raise OSError(_PRIVATE_ERROR)
            if stage == "create-collision":
                with original_open(target, "xb") as stream:
                    stream.write(_COLLISION_BYTES)
                hits.append(stage)
                # Exercise the actual exclusive open without overwriting bytes.
                return original_open(target, mode, *args, **kwargs)
            stream = original_open(target, mode, *args, **kwargs)
            if stage == "after-create":
                stream.close()
                hits.append(stage)
                raise OSError(_PRIVATE_ERROR)
            return stream
        return original_open(target, mode, *args, **kwargs)

    with monkeypatch.context() as isolated:
        isolated.setattr(Path, "mkdir", fault_mkdir)
        isolated.setattr(Path, "open", fault_open)
        _denied(host.seal)
    assert hits == [stage]
    data = (
        _COLLISION_BYTES
        if stage.endswith("collision")
        else b""
        if stage == "after-create"
        else None
    )
    _assert_evidence(host, before, directory=stage != "before-mkdir", data=data)
    if stage != "before-mkdir":
        _assert_partial_blocks(host)


@pytest.mark.parametrize(
    "fault", ["write-empty", "write-partial", "short-write", "flush", "file-fsync"]
)
def test_publish_stream_fault_never_grants_permission(
    host: Fixture,
    canonical_seal: bytes,
    fault: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _snapshot(host)
    path = host.seal_path()
    original_open, original_fsync = Path.open, oos.os.fsync
    hits: list[str] = []
    descriptors: list[int] = []

    def fault_open(target: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        stream = original_open(target, mode, *args, **kwargs)
        if target == path and mode == "xb":
            descriptors.append(stream.fileno())
            return _FaultingStream(stream, fault, hits)
        return stream

    def fault_fsync(descriptor: int) -> None:
        if fault == "file-fsync" and descriptor in descriptors:
            hits.append(fault)
            raise OSError(_PRIVATE_ERROR)
        original_fsync(descriptor)

    with monkeypatch.context() as isolated:
        isolated.setattr(Path, "open", fault_open)
        isolated.setattr(oos.os, "fsync", fault_fsync)
        _denied(host.seal)
    assert fault in hits
    if fault == "write-empty":
        expected = b""
    elif fault in {"write-partial", "short-write"}:
        expected = canonical_seal[: len(canonical_seal) // 2]
    else:
        expected = canonical_seal
    _assert_evidence(host, before, directory=True, data=expected)
    if expected == canonical_seal:
        _assert_exact_recovery(host, canonical_seal, monkeypatch)
    else:
        _assert_partial_blocks(host)


@pytest.mark.parametrize("directory", ["run", "root"])
def test_publish_directory_sync_failure_preserves_complete_spent_seal(
    host: Fixture,
    canonical_seal: bytes,
    directory: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _snapshot(host)
    target = host.seal_path().parent if directory == "run" else host.root
    original_sync = oos._sync_directory
    calls: list[Path] = []

    def fail_sync(path: Path) -> None:
        calls.append(path)
        if path == target:
            raise OSError(_PRIVATE_ERROR)
        original_sync(path)

    with monkeypatch.context() as isolated:
        # On Windows this tests propagation at the seam, not POSIX durability.
        isolated.setattr(oos, "_sync_directory", fail_sync)
        _denied(host.seal)
    assert calls[-1] == target
    _assert_evidence(host, before, directory=True, data=canonical_seal)
    _assert_exact_recovery(host, canonical_seal, monkeypatch)


@pytest.mark.parametrize(
    "fault", ["read-error", "parsed-mismatch", "truncated", "noncanonical"]
)
def test_publish_readback_failure_preserves_complete_spent_seal(
    host: Fixture,
    canonical_seal: bytes,
    fault: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _snapshot(host)
    path = host.seal_path()
    original_read, original_open = oos._read_seal, Path.open
    hits: list[str] = []

    def fault_read(target: Path, root: Path) -> dict[str, Any]:
        if target == path and fault in {"read-error", "parsed-mismatch"}:
            hits.append(fault)
            if fault == "read-error":
                raise OSError(_PRIVATE_ERROR)
            return {
                **original_read(target, root),
                "admitted_at": "2000-01-01T00:00:00Z",
            }
        return original_read(target, root)

    class FaultingReader:
        def __init__(self, stream: Any) -> None:
            self.stream = stream

        def __enter__(self) -> Self:
            self.stream.__enter__()
            return self

        def __exit__(self, *args: object) -> Any:
            return self.stream.__exit__(*args)

        def read(self, *args: Any) -> bytes:
            data = self.stream.read(*args)
            hits.append(fault)
            return data[: len(data) // 2] if fault == "truncated" else data + b"\n"

    def fault_open(target: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        stream = original_open(target, mode, *args, **kwargs)
        if target == path and mode == "rb" and fault in {"truncated", "noncanonical"}:
            return FaultingReader(stream)
        return stream

    with monkeypatch.context() as isolated:
        isolated.setattr(oos, "_read_seal", fault_read)
        isolated.setattr(Path, "open", fault_open)
        _denied(host.seal)
    assert hits == [fault]
    _assert_evidence(host, before, directory=True, data=canonical_seal)
    _assert_exact_recovery(host, canonical_seal, monkeypatch)


class _SimulatedCrash(BaseException):
    """Interrupt control flow without implying an ordinary recoverable error."""


@pytest.mark.parametrize("checkpoint", ["directory", "empty", "partial", "complete"])
def test_crash_evidence_survives_reopen_without_restored_freshness(
    host: Fixture,
    canonical_seal: bytes,
    checkpoint: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _snapshot(host)
    original_publish = oos._publish
    data = {
        "directory": None,
        "empty": b"",
        "partial": canonical_seal[: len(canonical_seal) // 2],
        "complete": canonical_seal,
    }[checkpoint]
    hits: list[str] = []

    def interrupted_publish(path: Path, seal: dict[str, Any]) -> None:
        assert path == host.seal_path()
        assert canonical_json(seal).encode("utf-8") == canonical_seal
        if checkpoint == "complete":
            original_publish(path, seal)
        else:
            path.parent.mkdir()
            if data is not None:
                with path.open("xb") as stream:
                    stream.write(data)
        hits.append(checkpoint)
        raise _SimulatedCrash

    with monkeypatch.context() as isolated:
        isolated.setattr(oos, "_publish", interrupted_publish)
        with pytest.raises(_SimulatedCrash):
            host.seal()
    assert hits == [checkpoint]
    _assert_evidence(host, before, directory=True, data=data)
    if checkpoint == "complete":
        _assert_exact_recovery(host, canonical_seal, monkeypatch)
    else:
        _assert_partial_blocks(host)


@pytest.mark.parametrize(
    "fault",
    [
        "sync-existing",
        "open-existing",
        "flush",
        "file-fsync",
        "run-sync",
        "root-sync",
        "readback",
    ],
)
def test_recovery_failure_preserves_bytes_and_never_restores_freshness(
    host: Fixture,
    canonical_seal: bytes,
    fault: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host.seal()
    host.reopen()
    before = _snapshot(host)
    original_sync, original_read = oos._sync_directory, oos._read_seal
    original_open = Path.open
    hits: list[str] = []
    reads = 0

    def fail(*args: Any, **kwargs: Any) -> None:
        hits.append(fault)
        raise OSError(_PRIVATE_ERROR)

    def fault_directory(path: Path) -> None:
        target = host.root if fault == "root-sync" else host.seal_path().parent
        if path == target:
            fail()
        original_sync(path)

    def fault_read(path: Path, root: Path) -> dict[str, Any]:
        nonlocal reads
        if path == host.seal_path():
            reads += 1
            if reads == 2:  # History read succeeds; post-sync readback fails.
                fail()
        return original_read(path, root)

    def fault_open(target: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if target == host.seal_path() and mode == "r+b" and fault == "open-existing":
            fail()
        stream = original_open(target, mode, *args, **kwargs)
        if target == host.seal_path() and mode == "r+b" and fault == "flush":
            return _FaultingStream(stream, fault, hits)
        return stream

    with monkeypatch.context() as isolated:
        if fault == "sync-existing":
            isolated.setattr(oos, "_sync_existing", fail)
        elif fault in {"open-existing", "flush"}:
            isolated.setattr(Path, "open", fault_open)
        elif fault == "file-fsync":
            isolated.setattr(oos.os, "fsync", fail)
        elif fault in {"run-sync", "root-sync"}:
            isolated.setattr(oos, "_sync_directory", fault_directory)
        else:
            isolated.setattr(oos, "_read_seal", fault_read)
        _denied(host.seal, mode="recovery")
    assert hits == [fault]
    assert _snapshot(host) == before
    _assert_exact_recovery(host, canonical_seal, monkeypatch)


def test_failure_before_publication_changes_no_bytes_or_store_records(
    host: Fixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _snapshot(host)
    hits: list[Path] = []

    def fail_publish(path: Path, seal: dict[str, Any]) -> None:
        hits.append(path)
        raise OSError(_PRIVATE_ERROR)

    with monkeypatch.context() as isolated:
        isolated.setattr(oos, "_publish", fail_publish)
        _denied(host.seal)
    assert hits == [host.seal_path()]
    assert _snapshot(host) == before
    host.reopen()
    assert _snapshot(host) == before
    _denied(host.seal, mode="recovery")
    _denied(host.verify)
    assert _snapshot(host) == before
