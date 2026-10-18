"""Exact encoded request sizing without reading credentials or sending HTTP."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from .gateway import canonical
from .protocols import ProtocolProvider


class _Captured(Exception):
    pass


class RequestPreview(ProtocolProvider):
    def __init__(self, *, protocol: str, model: str, generation_mode: str) -> None:
        super().__init__(
            "preview-only",
            protocol,
            "https://preview.example",
            model,
            secret=dict,
            generation_mode=generation_mode,
        )
        self.bounds: dict[str, int] | None = None

    def _request(
        self, suffix: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if payload is None:
            raise ValueError("only encoded structured payloads are previewed")
        output = payload.get(
            "max_tokens",
            payload.get(
                "max_completion_tokens",
                payload.get("generationConfig", {}).get("maxOutputTokens", 1800),
            ),
        )
        self.bounds = {
            "input": len(canonical(payload).encode()) + 2000,
            "output": output,
        }
        raise _Captured

    def estimate(
        self,
        context: dict[str, Any],
        schema: type[BaseModel],
        *,
        max_output: int = 1800,
    ) -> dict[str, int]:
        self.bounds = None
        try:
            self.complete_structured(context, schema, max_output=max_output)
        except _Captured:
            if self.bounds is not None:
                return self.bounds
        raise ValueError("request preview did not reach the blocked send boundary")
