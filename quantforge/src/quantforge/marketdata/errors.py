"""Deterministic PRICE-001 error taxonomy (frozen contract section 18)."""

from __future__ import annotations


class PriceEvidenceError(ValueError):
    """Base semantic error for the PRICE-001 price evidence boundary."""


class PriceArgumentError(PriceEvidenceError):
    """Malformed caller input, source conformance or request construction."""


class PriceSourceError(PriceEvidenceError):
    """A failure attributable to the injected price observation source."""


class PriceSourceRejectedError(PriceSourceError):
    """The injected source rejected the observation request."""


class PriceSourceIdentityError(PriceEvidenceError):
    """A source identity is malformed or contradicts the returned observation."""


class PriceObservationError(PriceEvidenceError):
    """The returned price observation is malformed or unverifiable."""


class PriceObservationIdentityError(PriceObservationError):
    """The observation identity does not re-derive from its own fields."""


class PriceCoverageError(PriceObservationError):
    """Symbol coverage is duplicated, missing, extra or not canonical."""


class PriceFactError(PriceObservationError):
    """A risk price fact or price value is invalid."""


class PriceProvenanceMismatchError(PriceEvidenceError):
    """The provenance is invalid or is not matched by the observation."""


class PriceTargetMismatchError(PriceEvidenceError):
    """The execution target is invalid or its submission identity is wrong."""


class PriceTimestampError(PriceEvidenceError):
    """A datetime is malformed, naive, out of range or not the frozen instant."""


class PriceFutureObservationError(PriceTimestampError):
    """The observation instant is later than the dispatch instant."""


class PriceStaleObservationError(PriceTimestampError):
    """The observation age exceeds its maximum age."""


class PriceEvidenceConstructionError(PriceEvidenceError):
    """The frozen evidence constructor rejected fully validated values."""


__all__ = [
    "PriceArgumentError",
    "PriceCoverageError",
    "PriceEvidenceConstructionError",
    "PriceEvidenceError",
    "PriceFactError",
    "PriceFutureObservationError",
    "PriceObservationError",
    "PriceObservationIdentityError",
    "PriceProvenanceMismatchError",
    "PriceSourceError",
    "PriceSourceIdentityError",
    "PriceSourceRejectedError",
    "PriceStaleObservationError",
    "PriceTargetMismatchError",
    "PriceTimestampError",
]
