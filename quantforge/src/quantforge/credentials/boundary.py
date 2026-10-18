"""Deterministic credential boundary implementing the frozen S0..S13 order.

The boundary is the only component that turns an installed backend into a
scoped lease.  Caller-controlled values are validated before the single
backend call, every returned value is revalidated afterwards and the raw
material is wrapped immediately into ``SecretMaterial``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .errors import (
    CredentialArgumentError,
    CredentialError,
    CredentialProviderError,
    CredentialProviderIdentityError,
    CredentialScopeMismatchError,
)
from .models import (
    _OPAQUE_PATTERN,
    CredentialFacts,
    CredentialReference,
    CredentialScope,
    _compute_facts_id,
    _utc_instant,
)
from .provider import _REJECTING_BACKEND, CredentialBackend
from .secrets import CredentialLease, SecretMaterial

_ZERO_OFFSET = timedelta(0)


class CredentialBoundary:
    """The only path that turns an installed backend into a scoped lease."""

    __slots__ = ("_backend",)

    def __init__(
        self,
        *,
        backend: CredentialBackend = _REJECTING_BACKEND,
    ) -> None:
        self._backend = backend

    def acquire(
        self,
        scope: CredentialScope,
        reference: CredentialReference,
        *,
        requested_at: datetime,
    ) -> CredentialLease:
        """Run the frozen S0..S13 acquisition sequence exactly once."""
        # S0 - argument conformance.
        if (
            type(scope) is not CredentialScope
            or type(reference) is not CredentialReference
            or not isinstance(requested_at, datetime)
        ):
            raise TypeError(
                "acquire requires an exact CredentialScope, an exact "
                "CredentialReference and a datetime instant"
            )
        backend = self._backend
        if backend is None:
            raise CredentialArgumentError(
                "no credential backend is installed"
            )
        conforming = True
        acquire_method: Any = None
        provider_id: Any = None
        try:
            acquire_method = backend.acquire
            provider_id = backend.provider_id
        except Exception:  # noqa: BLE001 - frozen S0 conformance probe
            conforming = False
        if not conforming or not callable(acquire_method):
            raise CredentialArgumentError(
                "backend does not satisfy the credential backend protocol"
            )
        # S1 - read provider_id exactly once and validate the opaque grammar.
        if (
            not isinstance(provider_id, str)
            or _OPAQUE_PATTERN.fullmatch(provider_id) is None
        ):
            raise CredentialProviderIdentityError(
                "provider identity is malformed"
            )
        # S2 - normalize requested_at.
        moment = _utc_instant(requested_at)
        # S3 - provider identity must match the requested scope.
        if scope.provider != provider_id:
            raise CredentialProviderIdentityError(
                "provider identity contradicts the requested scope"
            )
        # S4 - nothing else runs before the single backend call.
        # S5 - call the backend exactly once under the section 15 boundary.
        wrap_error: CredentialProviderError | None = None
        raw: Any = None
        try:
            raw = backend.acquire(scope, reference, requested_at=moment)
        except CredentialError:
            raise
        except Exception:  # noqa: BLE001 - frozen section 15 wrap case
            wrap_error = CredentialProviderError(
                f"credential provider {provider_id} failed"
            )
        except BaseException:
            raise
        if wrap_error is not None:
            raise wrap_error from None
        # S6 - the result must be a two-element tuple.
        if type(raw) is not tuple or len(raw) != 2:
            raise CredentialProviderError(
                "credential provider returned a malformed result"
            )
        # S7 - element 0 must be exactly a CredentialFacts instance.
        raw_facts = raw[0]
        if type(raw_facts) is not CredentialFacts:
            raise CredentialProviderError(
                "credential provider returned malformed facts"
            )
        # S8 - element 1 must be non-empty bytes or text; wrap it now.
        raw_material = raw[1]
        if (
            not isinstance(raw_material, (bytes, str))
            or len(raw_material) == 0
        ):
            raise CredentialProviderError(
                "credential provider returned no credential material"
            )
        material = SecretMaterial(raw_material)
        # The raw forms stay out of every later frame snapshot.
        del raw
        del raw_material
        # S9 - the facts identity must re-derive from the facts' own fields.
        if _compute_facts_id(raw_facts) != raw_facts.facts_id:
            raise CredentialProviderError(
                "credential facts identity does not re-derive"
            )
        # S10 - the served provider must be the validated provider identity.
        if raw_facts.provider != provider_id:
            raise CredentialProviderIdentityError(
                "credential facts name a different provider"
            )
        # S11 - exact scope, reference and acquisition instant.
        if raw_facts.scope != scope:
            raise CredentialScopeMismatchError(
                "credential facts do not match the requested scope"
            )
        if raw_facts.reference != reference:
            raise CredentialProviderError(
                "credential facts name a different reference"
            )
        if raw_facts.acquired_at != moment:
            raise CredentialProviderError(
                "credential facts carry a different acquisition instant"
            )
        # S12 - window, UTC normalization and generation.
        if raw_facts.expires_at <= raw_facts.acquired_at:
            raise CredentialProviderError(
                "credential facts carry an empty validity window"
            )
        if (
            raw_facts.acquired_at.utcoffset() != _ZERO_OFFSET
            or raw_facts.expires_at.utcoffset() != _ZERO_OFFSET
        ):
            raise CredentialProviderError(
                "credential facts instants are not UTC-normalized"
            )
        generation = raw_facts.generation
        if (
            isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 1
        ):
            raise CredentialProviderError(
                "credential facts generation is not a positive integer"
            )
        # S13 - construct the lease and return it.
        construction_failed = False
        lease: CredentialLease | None = None
        try:
            lease = CredentialLease(raw_facts, material)
        except CredentialError:
            raise
        except Exception:  # noqa: BLE001 - frozen S13 construction guard
            construction_failed = True
        if construction_failed or lease is None:
            raise CredentialError("credential lease construction failed")
        return lease
