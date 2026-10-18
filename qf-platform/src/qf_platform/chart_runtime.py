"""Explicit side-effect boundary for the durable screenshot application service."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from . import chart_algorithms, chart_provider
from .chart_reports import ChartReport


class ChartRuntime(Protocol):
    def algorithm(
        self,
        metadata: dict[str, Any],
        root: Path,
        *,
        initial_levels: dict[str, Any] | None,
    ) -> dict[str, Any]: ...
    def analyze(
        self,
        key: str,
        data: bytes,
        mime: str,
        metadata: dict[str, Any],
        algorithm: dict[str, Any] | None,
    ) -> tuple[ChartReport, dict[str, Any]]: ...
    def checkpoint(self, stage: str) -> None: ...


class LiveChartRuntime:
    def algorithm(
        self,
        metadata: dict[str, Any],
        root: Path,
        *,
        initial_levels: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return chart_algorithms.analyze(metadata, root, initial_levels=initial_levels)

    def analyze(
        self,
        key: str,
        data: bytes,
        mime: str,
        metadata: dict[str, Any],
        algorithm: dict[str, Any] | None,
    ) -> tuple[ChartReport, dict[str, Any]]:
        return chart_provider.analyze(key, data, mime, metadata, algorithm)

    def checkpoint(self, stage: str) -> None:
        """Production has no fault injection; offline adapters may interrupt here."""
