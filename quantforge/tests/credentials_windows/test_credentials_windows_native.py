"""C2 native oracles: ownership matrix, call counts, zeroization, platform."""

from __future__ import annotations

import ctypes
import sys
from typing import Any

import pytest
from _credentials_windows_fixtures import (
    CANARY_BYTES,
    GUARD_BYTE,
    USER_NAME,
    RecordingFree,
    compose_target,
    guarded_credential,
    make_reference,
    make_scope,
)

import quantforge.credentials_windows.native as native_module
from quantforge.credentials import (
    CredentialProviderError,
    CredentialUnavailableError,
)
from quantforge.credentials_windows.native import (
    Advapi32CredentialReadSeam,
    _Credential,
    _CredentialFailureKind,
    _generation_from_user_name,
    _NativeCredentialFailure,
    _NativeCredentialRecord,
)

pytestmark = pytest.mark.skipif(
    not native_module._platform_supported(),
    reason="requires the frozen 64-bit Windows interpreter",
)

PLATFORM_MESSAGE = "credential store is unavailable on this platform"
READ_MESSAGE = "credential store read failed"


def _slot(byref_out: Any) -> Any:
    return ctypes.cast(
        byref_out, ctypes.POINTER(ctypes.POINTER(_Credential))
    )


def _seam(free: Any) -> Advapi32CredentialReadSeam:
    seam = Advapi32CredentialReadSeam()
    seam._free = free
    return seam


def _install(
    seam: Advapi32CredentialReadSeam,
    behaviour: Any,
) -> None:
    seam._read = behaviour


def _target() -> str:
    return compose_target(make_scope(), make_reference())


def test_true_non_null_owns_zeroes_and_frees_exactly_once() -> None:
    target = _target()
    credential, buffer, pointer = guarded_credential(
        CANARY_BYTES, target_name=target
    )
    free = RecordingFree()
    seam = _seam(free)
    size = len(CANARY_BYTES)

    def behaviour(_target: str, *_args: Any) -> int:
        _slot(_args[-1])[0] = pointer
        return 1

    _install(seam, behaviour)
    outcome = seam.read(target)

    assert isinstance(outcome, _NativeCredentialRecord)
    assert outcome.blob == CANARY_BYTES
    assert outcome.target_name == target
    assert outcome.user_name == USER_NAME
    assert free.calls == 1
    assert bytes(buffer[1 : size + 1]) == bytes(size)
    assert buffer[0] == GUARD_BYTE
    assert buffer[size + 1] == GUARD_BYTE
    assert credential.CredentialBlobSize == size


def test_true_null_is_contradiction_without_free() -> None:
    free = RecordingFree()
    seam = _seam(free)

    def behaviour(_target: str, *_args: Any) -> int:
        return 1

    _install(seam, behaviour)
    outcome = seam.read(_target())

    assert isinstance(outcome, _NativeCredentialFailure)
    assert outcome.kind is _CredentialFailureKind.MALFORMED
    assert free.calls == 0


@pytest.mark.parametrize(
    ("status", "expected"),
    (
        (1168, _CredentialFailureKind.MISSING),
        (5, _CredentialFailureKind.ACCESS_DENIED),
        (1312, _CredentialFailureKind.ACCESS_DENIED),
        (1326, _CredentialFailureKind.ACCESS_DENIED),
        (87, _CredentialFailureKind.NATIVE_ERROR),
    ),
)
def test_false_null_maps_numeric_status(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    expected: _CredentialFailureKind,
) -> None:
    free = RecordingFree()
    seam = _seam(free)

    def behaviour(_target: str, *_args: Any) -> int:
        return 0

    _install(seam, behaviour)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: status)
    outcome = seam.read(_target())

    assert isinstance(outcome, _NativeCredentialFailure)
    assert outcome.kind is expected
    assert free.calls == 0


def test_false_non_null_is_contradiction_without_free_or_touch() -> None:
    target = _target()
    _credential, buffer, pointer = guarded_credential(
        CANARY_BYTES, target_name=target
    )
    free = RecordingFree()
    seam = _seam(free)
    size = len(CANARY_BYTES)

    def behaviour(_target: str, *_args: Any) -> int:
        _slot(_args[-1])[0] = pointer
        return 0

    _install(seam, behaviour)
    outcome = seam.read(target)

    assert isinstance(outcome, _NativeCredentialFailure)
    assert outcome.kind is _CredentialFailureKind.MALFORMED
    assert free.calls == 0
    assert bytes(buffer[1 : size + 1]) == CANARY_BYTES
    assert buffer[0] == GUARD_BYTE
    assert buffer[size + 1] == GUARD_BYTE


def test_every_acquisition_uses_a_fresh_null_out_slot() -> None:
    target = _target()
    _credential, _buffer, pointer = guarded_credential(
        CANARY_BYTES, target_name=target
    )
    free = RecordingFree()
    seam = _seam(free)
    observed: list[bool] = []

    def behaviour(_target: str, *_args: Any) -> int:
        slot = _slot(_args[-1])
        observed.append(bool(slot[0]))
        slot[0] = pointer
        return 1

    _install(seam, behaviour)
    first = seam.read(target)
    second = seam.read(target)

    assert isinstance(first, _NativeCredentialRecord)
    assert isinstance(second, _NativeCredentialRecord)
    assert observed == [False, False]
    assert free.calls == 2


@pytest.mark.parametrize(
    "mutate",
    (
        lambda credential: setattr(credential, "Flags", 1),
        lambda credential: setattr(credential, "Type", 2),
        lambda credential: setattr(credential, "Persist", 1),
        lambda credential: setattr(credential, "AttributeCount", 1),
        lambda credential: setattr(credential, "TargetAlias", "alias"),
        lambda credential: setattr(credential, "TargetName", "other"),
        lambda credential: setattr(credential, "UserName", "quantforge:g1"),
    ),
)
def test_structure_invariants_fail_closed(mutate: Any) -> None:
    target = _target()
    credential, _buffer, pointer = guarded_credential(
        CANARY_BYTES, target_name=target
    )
    mutate(credential)
    free = RecordingFree()
    seam = _seam(free)

    def behaviour(_target: str, *_args: Any) -> int:
        _slot(_args[-1])[0] = pointer
        return 1

    _install(seam, behaviour)
    outcome = seam.read(target)

    assert isinstance(outcome, _NativeCredentialFailure)
    assert outcome.kind is _CredentialFailureKind.MALFORMED
    assert free.calls == 1


def test_case_only_target_name_mismatch_fails_closed() -> None:
    target = _target()
    _credential, _buffer, pointer = guarded_credential(
        CANARY_BYTES, target_name=target.lower()
    )
    free = RecordingFree()
    seam = _seam(free)

    def behaviour(_target: str, *_args: Any) -> int:
        _slot(_args[-1])[0] = pointer
        return 1

    _install(seam, behaviour)
    outcome = seam.read(target)

    assert isinstance(outcome, _NativeCredentialFailure)
    assert outcome.kind is _CredentialFailureKind.MALFORMED
    assert free.calls == 1


@pytest.mark.parametrize("size", (1, 2560))
def test_blob_bounds_are_accepted_byte_exact(size: int) -> None:
    payload = bytes((index * 7) % 256 for index in range(size))
    target = _target()
    _credential, buffer, pointer = guarded_credential(
        payload, target_name=target
    )
    free = RecordingFree()
    seam = _seam(free)

    def behaviour(_target: str, *_args: Any) -> int:
        _slot(_args[-1])[0] = pointer
        return 1

    _install(seam, behaviour)
    outcome = seam.read(target)

    assert isinstance(outcome, _NativeCredentialRecord)
    assert outcome.blob == payload
    assert free.calls == 1
    assert bytes(buffer[1 : size + 1]) == bytes(size)
    assert buffer[0] == GUARD_BYTE
    assert buffer[size + 1] == GUARD_BYTE


@pytest.mark.parametrize("size", (0, 2561))
def test_out_of_bounds_blob_sizes_fail_closed(size: int) -> None:
    target = _target()
    credential, _buffer, pointer = guarded_credential(
        CANARY_BYTES, target_name=target
    )
    credential.CredentialBlobSize = size
    free = RecordingFree()
    seam = _seam(free)

    def behaviour(_target: str, *_args: Any) -> int:
        _slot(_args[-1])[0] = pointer
        return 1

    _install(seam, behaviour)
    outcome = seam.read(target)

    assert isinstance(outcome, _NativeCredentialFailure)
    assert outcome.kind is _CredentialFailureKind.MALFORMED
    assert free.calls == 1


def test_null_blob_pointer_with_nonzero_size_fails_closed() -> None:
    target = _target()
    credential, _buffer, pointer = guarded_credential(
        CANARY_BYTES, target_name=target
    )
    credential.CredentialBlob = ctypes.POINTER(ctypes.c_ubyte)()
    free = RecordingFree()
    seam = _seam(free)

    def behaviour(_target: str, *_args: Any) -> int:
        _slot(_args[-1])[0] = pointer
        return 1

    _install(seam, behaviour)
    outcome = seam.read(target)

    assert isinstance(outcome, _NativeCredentialFailure)
    assert outcome.kind is _CredentialFailureKind.MALFORMED
    assert free.calls == 1


def test_success_path_zero_failure_is_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target()
    _credential, buffer, pointer = guarded_credential(
        CANARY_BYTES, target_name=target
    )
    free = RecordingFree()
    seam = _seam(free)
    size = len(CANARY_BYTES)
    attempts: list[int] = []

    def failing_overwrite(_pointer: Any, attempt_size: int) -> None:
        attempts.append(attempt_size)
        raise OSError("synthetic overwrite failure")

    def behaviour(_target: str, *_args: Any) -> int:
        _slot(_args[-1])[0] = pointer
        return 1

    _install(seam, behaviour)
    monkeypatch.setattr(native_module, "_overwrite_blob", failing_overwrite)
    outcome = seam.read(target)

    assert isinstance(outcome, _NativeCredentialRecord)
    assert outcome.blob == CANARY_BYTES
    assert attempts == [size]
    assert free.calls == 1
    assert bytes(buffer[1 : size + 1]) == CANARY_BYTES
    assert buffer[0] == GUARD_BYTE
    assert buffer[size + 1] == GUARD_BYTE


def test_pending_error_zero_failure_keeps_original_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _target()
    _credential, _buffer, pointer = guarded_credential(
        CANARY_BYTES, target_name=target
    )
    free = RecordingFree()
    seam = _seam(free)
    attempts: list[int] = []

    def failing_copy(_pointer: Any, _size: int) -> bytes:
        raise OSError("synthetic copy failure")

    def failing_overwrite(_pointer: Any, attempt_size: int) -> None:
        attempts.append(attempt_size)
        raise OSError("synthetic overwrite failure")

    def behaviour(_target: str, *_args: Any) -> int:
        _slot(_args[-1])[0] = pointer
        return 1

    _install(seam, behaviour)
    monkeypatch.setattr(native_module, "_copy_blob", failing_copy)
    monkeypatch.setattr(native_module, "_overwrite_blob", failing_overwrite)

    with pytest.raises(CredentialProviderError) as info:
        seam.read(target)

    assert str(info.value) == READ_MESSAGE
    assert attempts == [len(CANARY_BYTES)]
    assert free.calls == 1
    assert info.value.__cause__ is None
    assert info.value.__context__ is None
    assert info.value.__suppress_context__ is True
    assert not getattr(info.value, "__notes__", None)


def test_unsupported_platform_construction_and_read_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seam = _seam(RecordingFree())
    called: list[int] = []

    def behaviour(_target: str, *_args: Any) -> int:
        called.append(1)
        return 0

    _install(seam, behaviour)
    monkeypatch.setattr(sys, "platform", "linux")

    with pytest.raises(CredentialUnavailableError) as info:
        seam.read(_target())
    assert str(info.value) == PLATFORM_MESSAGE
    assert called == []

    with pytest.raises(CredentialUnavailableError) as info:
        seam._invoke(_target())
    assert str(info.value) == PLATFORM_MESSAGE
    assert called == []

    created: list[int] = []

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        created.append(1)
        raise AssertionError("no native handle may be created")

    monkeypatch.setattr(native_module.ctypes, "WinDLL", forbidden)
    with pytest.raises(CredentialUnavailableError) as info:
        Advapi32CredentialReadSeam()
    assert str(info.value) == PLATFORM_MESSAGE
    assert created == []


@pytest.mark.parametrize(
    ("user_name", "expected"),
    (
        ("quantforge-crd-v1:g1", 1),
        ("quantforge-crd-v1:g42", 42),
        ("quantforge-crd-v1:g9999999999", 9999999999),
        ("quantforge-crd-v1:g0", None),
        ("quantforge-crd-v1:g01", None),
        ("quantforge-crd-v1:g", None),
        ("quantforge-crd-v1:g 1", None),
        ("quantforge-crd-v1:g1 ", None),
        ("quantforge-crd-v1:g-1", None),
        ("quantforge-crd-v1:g+1", None),
        ("quantforge-crd-v1:g1.0", None),
        ("quantforge-crd-v1:g10000000000", None),
        ("quantforge-crd-v1:g\u0661", None),
        ("", None),
    ),
)
def test_generation_grammar_is_exact(
    user_name: str, expected: int | None
) -> None:
    assert _generation_from_user_name(user_name) == expected


def test_read_uses_exactly_one_native_call_per_acquisition() -> None:
    target = _target()
    _credential, _buffer, pointer = guarded_credential(
        CANARY_BYTES, target_name=target
    )
    free = RecordingFree()
    seam = _seam(free)
    calls: list[str] = []

    def behaviour(target_name: str, *_args: Any) -> int:
        calls.append(target_name)
        _slot(_args[-1])[0] = pointer
        return 1

    _install(seam, behaviour)
    seam.read(target)

    assert calls == [target]
    assert free.calls == 1
