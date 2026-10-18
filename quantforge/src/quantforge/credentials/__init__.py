"""Deterministic credential isolation boundary for future integrations.

The package holds no real credential, discovers nothing ambiently, performs
no network work and ships only the rejecting backend.  See
``tasks/CREDENTIAL-001.md`` for the frozen contract.
"""

from __future__ import annotations

from .boundary import CredentialBoundary
from .errors import (
    CredentialArgumentError,
    CredentialError,
    CredentialLeaseError,
    CredentialLeaseExpiredError,
    CredentialProviderError,
    CredentialProviderIdentityError,
    CredentialRejectedError,
    CredentialScopeMismatchError,
    CredentialUnavailableError,
)
from .models import (
    CredentialFacts,
    CredentialPurpose,
    CredentialReference,
    CredentialScope,
)
from .provider import CredentialBackend, RejectingCredentialBackend
from .secrets import CredentialLease, SecretMaterial

# The frozen surface order is authoritative; it is not alphabetical.
__all__ = [  # noqa: RUF022
    "CredentialScope",
    "CredentialReference",
    "CredentialFacts",
    "CredentialPurpose",
    "CredentialBackend",
    "RejectingCredentialBackend",
    "CredentialBoundary",
    "CredentialLease",
    "SecretMaterial",
    "CredentialError",
    "CredentialArgumentError",
    "CredentialProviderError",
    "CredentialProviderIdentityError",
    "CredentialUnavailableError",
    "CredentialRejectedError",
    "CredentialScopeMismatchError",
    "CredentialLeaseError",
    "CredentialLeaseExpiredError",
]
