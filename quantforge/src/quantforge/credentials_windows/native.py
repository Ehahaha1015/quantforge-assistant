"""Narrow Windows native seam for the CREDENTIAL-002 credential backend.

The seam resolves exactly two advapi32 exports (CredReadW and CredFree),
validates the returned native structure, copies the raw credential bytes
into Python-owned bytes, performs one bounded best-effort overwrite and
frees the native allocation exactly once iff ownership was established.
It never logs, never renders native data and never raises unsanitized
native detail.  See ``tasks/CREDENTIAL-002.md`` for the frozen contract.
"""

from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final, Protocol

from quantforge.credentials import CredentialProviderError, CredentialUnavailableError

_CRED_TYPE_GENERIC: Final = 1
_CRED_PERSIST_LOCAL_MACHINE: Final = 2
_CRED_MAX_CREDENTIAL_BLOB_SIZE: Final = 2560
_CRED_BLOB_MINIMUM: Final = 1
_ERROR_NOT_FOUND: Final = 1168
_ERROR_ACCESS_DENIED: Final = 5
_ERROR_NO_SUCH_LOGON_SESSION: Final = 1312
_ERROR_LOGON_FAILURE: Final = 1326
_SUPPORTED_PLATFORM: Final = "win32"
_SUPPORTED_POINTER_SIZE: Final = 8
_USER_NAME_PREFIX: Final = "quantforge-crd-v1:g"
_GENERATION_MAX_DIGITS: Final = 10
_GENERATION_DIGITS: Final = "0123456789"
_PLATFORM_MESSAGE: Final = "credential store is unavailable on this platform"
_STORE_MESSAGE: Final = "credential store is unavailable"
_READ_MESSAGE: Final = "credential store read failed"
_RECORD_TEXT: Final = "<native credential record redacted>"
_FAILURE_TEXT: Final = "<native credential failure redacted>"
_SEAM_TEXT: Final = "<Advapi32CredentialReadSeam redacted>"


class _FileTime(ctypes.Structure):
    """Windows FILETIME; declared for structural fidelity only."""

    _fields_ = (
        ("dwLowDateTime", ctypes.c_uint32),
        ("dwHighDateTime", ctypes.c_uint32),
    )


class _CredentialAttribute(ctypes.Structure):
    """Windows CREDENTIAL_ATTRIBUTEW; never propagated."""

    _fields_ = (
        ("Keyword", ctypes.c_wchar_p),
        ("Flags", ctypes.c_uint32),
        ("ValueSize", ctypes.c_uint32),
        ("Value", ctypes.POINTER(ctypes.c_ubyte)),
    )


class _Credential(ctypes.Structure):
    """Windows CREDENTIALW declared with ctypes fields only."""

    _fields_ = (
        ("Flags", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", _FileTime),
        ("CredentialBlobSize", ctypes.c_uint32),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", ctypes.c_uint32),
        ("AttributeCount", ctypes.c_uint32),
        ("Attributes", ctypes.POINTER(_CredentialAttribute)),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    )


class _CredentialFailureKind(Enum):
    """Internal failure classification; never rendered to a caller."""

    MISSING = "MISSING"
    ACCESS_DENIED = "ACCESS_DENIED"
    STORE_UNAVAILABLE = "STORE_UNAVAILABLE"
    MALFORMED = "MALFORMED"
    NATIVE_ERROR = "NATIVE_ERROR"


@dataclass(frozen=True, slots=True, repr=False)
class _NativeCredentialRecord:
    """Validated native result: exactly Type, TargetName, Persist, UserName
    and the copied CredentialBlob bytes."""

    type_value: int
    target_name: str
    persist: int
    user_name: str
    blob: bytes

    def __repr__(self) -> str:
        return _RECORD_TEXT

    def __str__(self) -> str:
        return _RECORD_TEXT


@dataclass(frozen=True, slots=True, repr=False)
class _NativeCredentialFailure:
    """Validated native failure: exactly one failure kind, no native text."""

    kind: _CredentialFailureKind

    def __repr__(self) -> str:
        return _FAILURE_TEXT

    def __str__(self) -> str:
        return _FAILURE_TEXT


class CredentialReadSeam(Protocol):
    """The only native abstraction: one explicit read per acquisition."""

    def read(
        self, target_name: str
    ) -> _NativeCredentialRecord | _NativeCredentialFailure:
        ...


def _platform_supported() -> bool:
    """Return True only on the frozen 64-bit Windows interpreter."""
    if sys.platform != _SUPPORTED_PLATFORM:
        return False
    return ctypes.sizeof(ctypes.c_void_p) == _SUPPORTED_POINTER_SIZE


def _generation_from_user_name(user_name: str) -> int | None:
    """Return the frozen generation for an exact UserName, else None."""
    if not isinstance(user_name, str):
        return None
    prefix_length = len(_USER_NAME_PREFIX)
    if len(user_name) <= prefix_length:
        return None
    if user_name[:prefix_length] != _USER_NAME_PREFIX:
        return None
    digits = user_name[prefix_length:]
    if len(digits) > _GENERATION_MAX_DIGITS:
        return None
    if digits[0] == "0":
        return None
    for character in digits:
        if character not in _GENERATION_DIGITS:
            return None
    return int(digits)


def _overwrite_blob(pointer: Any, size: int) -> None:
    """Best-effort overwrite of exactly the validated blob region."""
    ctypes.memset(pointer, 0, size)


def _copy_blob(pointer: Any, size: int) -> bytes:
    """Copy exactly the validated blob region into Python-owned bytes."""
    return ctypes.string_at(pointer, size)


def _classified_failure(status: int) -> _NativeCredentialFailure:
    """Map a numeric GetLastError value to one sanitized failure kind."""
    if status == _ERROR_NOT_FOUND:
        kind = _CredentialFailureKind.MISSING
    elif status in (
        _ERROR_ACCESS_DENIED,
        _ERROR_NO_SUCH_LOGON_SESSION,
        _ERROR_LOGON_FAILURE,
    ):
        kind = _CredentialFailureKind.ACCESS_DENIED
    else:
        kind = _CredentialFailureKind.NATIVE_ERROR
    return _NativeCredentialFailure(kind)


class Advapi32CredentialReadSeam:
    """The production seam: CredReadW, CredFree and GetLastError only."""

    __slots__ = ("_free", "_library", "_read")

    # ctypes export values are dynamic; construction binds them only on Windows.
    _free: Any
    _library: Any
    _read: Any

    def __init__(self) -> None:
        if sys.platform != "win32" or not _platform_supported():
            raise CredentialUnavailableError(_PLATFORM_MESSAGE)
        error: CredentialUnavailableError | None = None
        try:
            self._library = ctypes.WinDLL("advapi32", use_last_error=True)
            self._read = self._library.CredReadW
            self._read.restype = ctypes.c_int
            self._read.argtypes = [
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.POINTER(_Credential)),
            ]
            self._free = self._library.CredFree
            self._free.restype = None
            self._free.argtypes = [ctypes.c_void_p]
        except Exception:  # noqa: BLE001 - frozen conversion to typed error
            error = CredentialUnavailableError(_STORE_MESSAGE)
        except BaseException:
            raise
        if error is not None:
            raise error from None

    def read(
        self, target_name: str
    ) -> _NativeCredentialRecord | _NativeCredentialFailure:
        if not _platform_supported():
            raise CredentialUnavailableError(_PLATFORM_MESSAGE)
        conversion_error: CredentialProviderError | None = None
        outcome: _NativeCredentialRecord | _NativeCredentialFailure | None = None
        try:
            outcome = self._invoke(target_name)
        except Exception:  # noqa: BLE001 - frozen conversion to typed error
            conversion_error = CredentialProviderError(_READ_MESSAGE)
        except BaseException:
            raise
        if conversion_error is not None:
            raise conversion_error from None
        if outcome is None:
            return _NativeCredentialFailure(_CredentialFailureKind.NATIVE_ERROR)
        return outcome

    def _invoke(
        self, target_name: str
    ) -> _NativeCredentialRecord | _NativeCredentialFailure:
        if sys.platform != "win32":
            raise CredentialUnavailableError(_PLATFORM_MESSAGE)
        out: Any = ctypes.POINTER(_Credential)()
        returned = self._read(
            target_name,
            _CRED_TYPE_GENERIC,
            0,
            ctypes.byref(out),
        )
        if not returned:
            status = int(ctypes.get_last_error())
            if out:
                return _NativeCredentialFailure(_CredentialFailureKind.MALFORMED)
            return _classified_failure(status)
        if not out:
            return _NativeCredentialFailure(_CredentialFailureKind.MALFORMED)
        return self._consume(out, target_name)

    def _consume(
        self,
        out: Any,
        target_name: str,
    ) -> _NativeCredentialRecord | _NativeCredentialFailure:
        record = None
        failure = None
        pending = None
        free_error = None
        try:
            credential = out.contents
            pointer = credential.CredentialBlob
            size = int(credential.CredentialBlobSize)
            bounded = (
                _CRED_BLOB_MINIMUM <= size <= _CRED_MAX_CREDENTIAL_BLOB_SIZE
            )
            if bounded and bool(pointer):
                try:
                    blob = _copy_blob(pointer, size)
                except Exception:  # noqa: BLE001 - pending typed error set
                    blob = None
                    pending = CredentialProviderError(_READ_MESSAGE)
                try:
                    _overwrite_blob(pointer, size)
                except Exception:  # noqa: BLE001, S110 - best effort swallow
                    pass
                if pending is None:
                    record = self._validated_record(
                        credential, blob, target_name
                    )
                    if record is None:
                        failure = _CredentialFailureKind.MALFORMED
            else:
                failure = _CredentialFailureKind.MALFORMED
        finally:
            try:
                self._free(out)
            except Exception:  # noqa: BLE001 - free failure recorded
                free_error = CredentialProviderError(_READ_MESSAGE)
        if pending is not None:
            raise pending from None
        if free_error is not None:
            raise free_error from None
        if failure is not None:
            return _NativeCredentialFailure(failure)
        if record is None:
            return _NativeCredentialFailure(_CredentialFailureKind.MALFORMED)
        return record

    def _validated_record(
        self,
        credential: _Credential,
        blob: bytes | None,
        target_name: str,
    ) -> _NativeCredentialRecord | None:
        if blob is None:
            return None
        if int(credential.Flags) != 0:
            return None
        if int(credential.Type) != _CRED_TYPE_GENERIC:
            return None
        if int(credential.Persist) != _CRED_PERSIST_LOCAL_MACHINE:
            return None
        if int(credential.AttributeCount) != 0:
            return None
        if bool(credential.Attributes):
            return None
        if credential.TargetAlias is not None:
            return None
        if credential.TargetName != target_name:
            return None
        user_name = credential.UserName
        if user_name is None:
            return None
        if _generation_from_user_name(user_name) is None:
            return None
        return _NativeCredentialRecord(
            type_value=int(credential.Type),
            target_name=target_name,
            persist=int(credential.Persist),
            user_name=user_name,
            blob=blob,
        )

    def __repr__(self) -> str:
        return _SEAM_TEXT

    def __str__(self) -> str:
        return _SEAM_TEXT
