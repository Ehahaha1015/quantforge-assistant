"""Current-user Windows DPAPI. No plaintext fallback or printable secret objects."""

from __future__ import annotations

import ctypes
import sys
from typing import Protocol

from .storage import PlatformError


class Protector(Protocol):
    def protect(self, plaintext: bytes) -> bytes: ...

    def unprotect(self, ciphertext: bytes) -> bytes: ...


class _Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]


class WindowsProtector:
    """DPAPI scoped to the signed-in user, with UI prompts disabled."""

    def _crypt(self, value: bytes, *, decrypt: bool) -> bytes:
        if sys.platform != "win32":
            raise PlatformError("OS_SECRET_STORE_UNAVAILABLE")
        if not value or len(value) > 65536:
            raise PlatformError("SECRET_SIZE_INVALID")
        crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
        source, result = _Blob(len(value), buffer), _Blob()
        method = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
        method.argtypes = [
            ctypes.POINTER(_Blob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(_Blob),
        ]
        method.restype = ctypes.c_int
        # CRYPTPROTECT_UI_FORBIDDEN; no LOCAL_MACHINE flag.
        if not method(
            ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)
        ):
            raise PlatformError("OS_SECRET_STORE_DENIED")
        try:
            return ctypes.string_at(result.data, result.size)
        finally:
            kernel.LocalFree(result.data)

    def protect(self, plaintext: bytes) -> bytes:
        return self._crypt(plaintext, decrypt=False)

    def unprotect(self, ciphertext: bytes) -> bytes:
        return self._crypt(ciphertext, decrypt=True)
