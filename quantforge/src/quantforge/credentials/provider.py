"""Credential backend seam and the shipped rejecting default.

The protocol is the explicit injection point.  The only concrete backend
shipped by CREDENTIAL-001 rejects every acquisition and holds no state.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from .errors import CredentialUnavailableError
from .models import CredentialFacts, CredentialReference, CredentialScope


class CredentialBackend(Protocol):
    """Explicit provider seam for exactly one credential acquisition."""

    @property
    def provider_id(self) -> str:
        """Explicit, non-empty opaque provider identity."""
        ...

    def acquire(
        self,
        scope: CredentialScope,
        reference: CredentialReference,
        *,
        requested_at: datetime,
    ) -> tuple[CredentialFacts, bytes | str]:
        """Return one acquisition's safe facts plus the raw secret."""
        ...


class RejectingCredentialBackend:
    """Stateless shipped backend that rejects every acquisition."""

    @property
    def provider_id(self) -> str:
        return "rejecting-credential-backend-v1"

    def acquire(
        self,
        scope: CredentialScope,
        reference: CredentialReference,
        *,
        requested_at: datetime,
    ) -> tuple[CredentialFacts, bytes | str]:
        raise CredentialUnavailableError("no credential backend is installed")


_REJECTING_BACKEND = RejectingCredentialBackend()
