"""Injected OPERATOR-001 authentication verifier boundary."""

from __future__ import annotations

from typing import Protocol

from .errors import OperatorAuthenticationError
from .models import (
    OperatorAuthenticationProof,
    OperatorControlRequest,
    VerifiedOperatorIdentity,
)


class OperatorVerifier(Protocol):
    @property
    def verifier_id(self) -> str: ...

    def verify(
        self,
        request: OperatorControlRequest,
        proof: OperatorAuthenticationProof,
    ) -> VerifiedOperatorIdentity: ...


class RejectingOperatorVerifier:
    """The only shipped verifier: it rejects every authentication input."""

    @property
    def verifier_id(self) -> str:
        return "rejecting-operator-verifier-v1"

    def verify(
        self,
        request: OperatorControlRequest,
        proof: OperatorAuthenticationProof,
    ) -> VerifiedOperatorIdentity:
        raise OperatorAuthenticationError("operator verifier rejected the proof")


__all__ = ["OperatorVerifier", "RejectingOperatorVerifier"]
