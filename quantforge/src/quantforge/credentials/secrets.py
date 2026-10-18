"""Non-serializable, non-copyable secret material and credential lease.

Both classes use ``__slots__``, render only redacted text, refuse every copy
or serialization path and never claim physical zeroization.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, NoReturn, SupportsIndex

from .errors import CredentialLeaseError, CredentialLeaseExpiredError
from .models import CredentialFacts, _utc_instant


# ``__slots__`` plus no explicit base means exactly ``object`` is inherited.
class SecretMaterial:
    """Boundary-issued wrapper for raw secret bytes with logical discard."""

    __slots__ = ("_buffer", "_discarded")

    def __init__(self, raw: bytes | str) -> None:
        if isinstance(raw, bytes):
            self._buffer = bytearray(raw)
        elif isinstance(raw, str):
            self._buffer = bytearray(raw.encode("utf-8"))
        else:
            raise CredentialLeaseError(
                "secret material requires bytes or text"
            )
        self._discarded = False

    def as_bytes(self) -> bytes:
        """Return a fresh raw bytes copy for immediate external use."""
        if self._discarded:
            raise CredentialLeaseError("secret material is discarded")
        return bytes(self._buffer)

    def discard(self) -> None:
        """Zero the internal buffer where possible; idempotent."""
        if self._discarded:
            return
        for index in range(len(self._buffer)):
            self._buffer[index] = 0
        self._buffer.clear()
        self._discarded = True

    def __repr__(self) -> str:
        return "<SecretMaterial redacted>"

    def __str__(self) -> str:
        return "<SecretMaterial redacted>"

    def __copy__(self) -> NoReturn:
        raise TypeError("SecretMaterial cannot be copied")

    def __deepcopy__(self, memo: Any) -> NoReturn:
        raise TypeError("SecretMaterial cannot be deep-copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("SecretMaterial cannot be serialized")

    # SupportsIndex is the LSP-compatible spelling of the frozen ``protocol``
    # parameter; the required behaviour is the TypeError below.
    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        raise TypeError("SecretMaterial cannot be serialized")


class CredentialLease:
    """Boundary-issued, secret-bearing capability with a logical lifetime."""

    __slots__ = ("_discarded", "_facts", "_material")

    def __init__(self, facts: CredentialFacts, material: SecretMaterial) -> None:
        self._facts = facts
        self._material = material
        self._discarded = False

    @property
    def facts(self) -> CredentialFacts:
        """Safe, non-secret facts of the acquisition this lease represents."""
        return self._facts

    def reveal(self, *, evaluation_time: datetime) -> SecretMaterial:
        """Return the boundary-issued material inside the lease window."""
        moment = _utc_instant(evaluation_time)
        if self._discarded:
            raise CredentialLeaseError("credential lease is discarded")
        if moment < self._facts.acquired_at:
            raise CredentialLeaseError("credential lease is not yet valid")
        if moment >= self._facts.expires_at:
            raise CredentialLeaseExpiredError("credential lease is expired")
        return self._material

    def discard(self) -> None:
        """Make the lease and its material unusable; idempotent."""
        if self._discarded:
            return
        self._discarded = True
        self._material.discard()

    def __repr__(self) -> str:
        return f"<CredentialLease redacted facts_id={self._facts.facts_id}>"

    def __str__(self) -> str:
        return self.__repr__()

    def __copy__(self) -> NoReturn:
        raise TypeError("CredentialLease cannot be copied")

    def __deepcopy__(self, memo: Any) -> NoReturn:
        raise TypeError("CredentialLease cannot be deep-copied")

    def __reduce__(self) -> NoReturn:
        raise TypeError("CredentialLease cannot be serialized")

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        raise TypeError("CredentialLease cannot be serialized")
