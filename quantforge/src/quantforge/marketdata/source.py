"""Injected PRICE-001 price observation source boundary (contract section 7)."""

from __future__ import annotations

from typing import Protocol

from .errors import PriceSourceRejectedError
from .models import PriceObservationRequest, VerifiedPriceObservation


class PriceObservationSource(Protocol):
    """Trust seam that attests one price observation per request."""

    @property
    def source_id(self) -> str: ...

    def observe(self, request: PriceObservationRequest) -> VerifiedPriceObservation: ...


class RejectingPriceObservationSource:
    """The only shipped source: it rejects every observation request."""

    @property
    def source_id(self) -> str:
        return "rejecting-price-observation-source-v1"

    def observe(self, request: PriceObservationRequest) -> VerifiedPriceObservation:
        raise PriceSourceRejectedError("price observation source rejected")


__all__ = ["PriceObservationSource", "RejectingPriceObservationSource"]
