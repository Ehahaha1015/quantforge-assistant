"""Host-pinned historical reports; verified projections, never model-selected paths."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl
from quantforge.data import logical_content_hash
from quantforge.product.storage import local_root

from .cash_archive import CashShareArchive
from .dataset_use import WORKSPACE, register_research_use
from .storage import digest


@dataclass(frozen=True)
class ArchiveSource:
    id: str
    title: str
    kind: str
    root: Path
    bars: tuple[Path, ...]
    plan_sha256: str | None = None
    result_sha256: str | None = None


def default_sources() -> tuple[ArchiveSource, ...]:
    first = WORKSPACE / "local-smoke/strategy-optimization-20261013/run-510300-v2"
    second = WORKSPACE / "local-smoke/trend-refinement-20261013/suite-v1"
    third = WORKSPACE / "local-smoke/long-term-20261013/weights/run-f56ae1c66a53"
    vintage = (
        WORKSPACE
        / "local-smoke/long-term-20261013/data-quality/vintage-20261013T063055-e0d2531f"
    )
    return (
        ArchiveSource(
            "optimization-510300-v2",
            "Rolling selection",
            "OPTIMIZATION",
            first,
            (first / "data/cnpilot-6159c44497b3310260d2946e/bars.parquet",),
        ),
        ArchiveSource(
            "trend-suite-v1",
            "Fixed trend ablations",
            "REFINEMENT",
            second,
            tuple(
                second / f"asset-{symbol}/data/{snapshot}/bars.parquet"
                for symbol, snapshot in (
                    ("510300", "cnpilot-6159c44497b3310260d2946e"),
                    ("510500", "cnpilot-7d18cf3c9e0040a1e67edb41"),
                    ("159915", "cnpilot-03982c2cd1032b3a48f84a96"),
                )
            ),
        ),
        ArchiveSource(
            "weights-510300-v1",
            "Decimal weights and risk",
            "WEIGHTS",
            third,
            (vintage / "510300-bars.parquet",),
        ),
        ArchiveSource(
            "cash-share-510500-v1",
            "Known cash and share correction",
            "CASH_SHARE",
            WORKSPACE
            / "local-smoke/cash-share-reconstructions/20261014T190921-a599ce41",
            (),
            "3a1df19018d4d366ecf1f26a78aa8ef0bfb9d2f99f9b3643470048270cd666b9",
            "8950622f3eac733a8a80e9e73ad8813203e819046659816b7489544fefd2fae5",
        ),
    )


def _bytes(path: Path, *, maximum: int = 32 * 1024 * 1024) -> bytes:
    local_root(path.parent)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise ValueError("ARCHIVE_REGULAR_BOUNDED_SOURCE_REQUIRED")
    raw = path.read_bytes()
    if len(raw) > maximum:
        raise ValueError("ARCHIVE_SOURCE_TOO_LARGE")
    return raw


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("ARCHIVE_FINITE_NUMBER_REQUIRED")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("ARCHIVE_FINITE_NUMBER_REQUIRED")
    return number


class ResearchArchive:
    def __init__(
        self,
        sources: tuple[ArchiveSource, ...] | None = None,
        *,
        usage_root: Path | None = None,
    ) -> None:
        entries = default_sources() if sources is None else sources
        if len({value.id for value in entries}) != len(entries) or any(
            not re.fullmatch(r"[a-z0-9-]{1,64}", value.id) for value in entries
        ):
            raise ValueError("ARCHIVE_ID_REQUIRED")
        self.sources = {value.id: value for value in entries}
        self.usage_root = usage_root

    def _source(self, id: str) -> ArchiveSource:
        if id not in self.sources:
            raise ValueError("ARCHIVE_NOT_REGISTERED")
        return self.sources[id]

    def _cash(self, source: ArchiveSource) -> CashShareArchive:
        return CashShareArchive(
            source.root,
            plan_hash=source.plan_sha256,
            result_hash=source.result_sha256,
            id=source.id,
            title=source.title,
            usage_root=self.usage_root,
        )

    def _load(
        self, id: str
    ) -> tuple[ArchiveSource, dict[str, Any], dict[str, Any], str]:
        source = self._source(id)
        plan = json.loads(_bytes(source.root / "plan.json"))
        result = json.loads(_bytes(source.root / "result.json"))
        checksum = json.loads(_bytes(source.root / "result-digest.json"))
        if digest(result) != checksum["sha256"] or digest(plan) != result["plan_id"]:
            raise ValueError("ARCHIVE_HASH_MISMATCH")
        schemas = {
            "OPTIMIZATION": "bounded-strategy-optimization-v1",
            "REFINEMENT": "fixed-trend-suite-v1",
            "WEIGHTS": "declared-weight-comparison-v1",
        }
        # Old report identities stay frozen even when the current Core has changed.
        if (
            result["schema_version"] != schemas[source.kind]
            or result["live_eligible"] is not False
            or result["validated_alpha"] is not False
        ):
            raise ValueError("ARCHIVE_QUALIFICATION_OR_SCHEMA_INVALID")
        return source, plan, result, digest(result)

    @staticmethod
    def _groups(source: ArchiveSource, result: dict[str, Any]) -> list[dict[str, Any]]:
        if source.kind == "WEIGHTS":
            return [
                {
                    "asset": next(iter(result["scenarios"].values()))["symbol"],
                    "key": "declared",
                    "fee_bps": None,
                    "extra_delay_bars": 0,
                    "strategies": [
                        {"strategy": name, **value}
                        for name, value in result["scenarios"].items()
                    ],
                }
            ]
        assets = (
            result["assets"]
            if source.kind == "REFINEMENT"
            else {result["symbol"]: result}
        )
        return [
            {
                "asset": asset,
                "key": f"{scenario['fee_bps']}-{scenario.get('extra_delay_bars', 0)}",
                "fee_bps": scenario["fee_bps"],
                "extra_delay_bars": scenario.get("extra_delay_bars", 0),
                "strategies": scenario["strategies"],
            }
            for asset, value in assets.items()
            for scenario in value["scenarios"]
        ]

    @staticmethod
    def _header(
        source: ArchiveSource,
        plan: dict[str, Any],
        result: dict[str, Any],
        result_hash: str,
    ) -> dict[str, Any]:
        groups = ResearchArchive._groups(source, result)
        return {
            "id": source.id,
            "title": source.title,
            "kind": source.kind,
            "mode": "PAPER_RESEARCH"
            if source.kind == "WEIGHTS"
            else "HISTORICAL_EXPLORATORY",
            "basis": "RAW_PLUS_KNOWN_CASH"
            if source.kind == "WEIGHTS"
            else "RAW_PRICE_ONLY",
            "assets": sorted({value["asset"] for value in groups}),
            "scenarios": [
                {key: value[key] for key in ("key", "fee_bps", "extra_delay_bars")}
                for value in groups
                if value["asset"] == groups[0]["asset"]
            ],
            "plan_hash": digest(plan),
            "result_hash": result_hash,
            "core_source_hash": plan["core_source_hash"],
            "formal_evidence_pack": False,
            "validated_alpha": False,
            "live_authority": "NONE",
            "start": result.get(
                "evaluation_start",
                groups[0]["strategies"][0]["periods"][plan.get("history_bars", 0)].get(
                    "session"
                ),
            ),
            "end": result.get(
                "evaluation_end",
                groups[0]["strategies"][0]["periods"][-1].get("session"),
            ),
        }

    def list(self) -> dict[str, Any]:
        items, issues = [], []
        for id in self.sources:
            try:
                source = self.sources[id]
                if source.kind == "CASH_SHARE":
                    reader = self._cash(source)
                    plan, result, _ = reader.load()
                    items.append(reader.header(plan, result))
                    continue
                source, plan, result, checksum = self._load(id)
                items.append(self._header(source, plan, result, checksum))
            except (ValueError, TypeError, KeyError, OSError):
                issues.append({"id": id, "error": "SOURCE_UNAVAILABLE_OR_UNVERIFIED"})
        return {
            "schema_version": "qf-archive-list-v1",
            "items": items,
            "issues": issues,
            "observed_at": datetime.now(UTC).isoformat(),
            "live_authority": "NONE",
        }

    def get(
        self, id: str, *, asset: str | None = None, scenario: str | None = None
    ) -> dict[str, Any]:
        source = self._source(id)
        if source.kind == "CASH_SHARE":
            return self._cash(source).get(asset=asset, scenario=scenario)
        source, plan, result, checksum = self._load(id)
        header = self._header(source, plan, result, checksum)
        selected_asset = asset if asset is not None else header["assets"][0]
        selected_scenario = (
            scenario
            if scenario is not None
            else ("declared" if source.kind == "WEIGHTS" else "10-0")
        )
        group = next(
            (
                item
                for item in self._groups(source, result)
                if item["asset"] == selected_asset and item["key"] == selected_scenario
            ),
            None,
        )
        if group is None:
            raise ValueError("ARCHIVE_GROUP_UNAVAILABLE")
        registered = self._register(source, result, checksum)
        rows = []
        for item in group["strategies"]:
            weighted = source.kind == "WEIGHTS"
            metrics = (
                {
                    "total_return": _number(item["net_return"]),
                    "max_drawdown": _number(item["max_drawdown"]),
                    "turnover": None,
                }
                if weighted
                else {
                    key: _number(item["metrics"][key])
                    for key in ("total_return", "max_drawdown", "turnover")
                }
            )
            periods = (
                item["periods"][plan["history_bars"] :] if weighted else item["periods"]
            )
            rows.append(
                {
                    "strategy": item["strategy"],
                    "metrics": metrics,
                    "exposure": _number(
                        item["exposure_summary"]["average_market_weight"]
                    )
                    if weighted
                    else item.get("exposure_fraction"),
                    "cash_fraction": _number(
                        item["exposure_summary"]["average_cash_fraction"]
                    )
                    if weighted
                    else None,
                    "flat_up_day_fraction": item.get("flat_up_day_fraction"),
                    "fee_total": item.get("fee_total"),
                    "fill_count": item.get("fill_count"),
                    "weight_breach_days": item.get("market_weight_breach_days"),
                    "periods": [
                        {
                            "date": row.get("session", row.get("timestamp")),
                            "equity": _number(row["equity"]),
                            "position": _number(row["weight_at_close"])
                            if weighted
                            else row.get("position_after_open"),
                            "quantity": row.get("quantity"),
                            "cash": row.get("cash"),
                            "fees": row.get("fees"),
                            "receivable": row.get("receivable"),
                        }
                        for row in periods
                    ],
                }
            )
        assets = result.get("assets", {})
        limitations = (
            next(iter(result["scenarios"].values()))["limitations"]
            if source.kind == "WEIGHTS"
            else assets[selected_asset]["limitations"]
            if source.kind == "REFINEMENT"
            else result["limitations"]
        )
        artifacts = []
        for name in ("plan", "result", "report", "metrics"):
            path = source.root / (
                name + {"report": ".md", "metrics": ".csv"}.get(name, ".json")
            )
            if path.exists():
                raw = _bytes(path)
                artifacts.append(
                    {
                        "id": name,
                        "filename": path.name,
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    }
                )
        return {
            **header,
            "schema_version": "qf-archive-report-v1",
            "asset": selected_asset,
            "scenario": selected_scenario,
            "rows": rows,
            "source": result.get(
                "source",
                {
                    "declaration": "Source-bound ETF vintage and issuer "
                    "cash announcements",
                    "adjustment": "Raw OHLCV with known cash distributions; "
                    "coverage incomplete",
                    "prior_exposure": "Exposed historical exploration; "
                    "not final confirmation",
                },
            ),
            "limitations": limitations,
            "artifacts": artifacts,
            "decisions": [
                {key: value for key, value in row.items() if key != "candidates"}
                for row in result.get("decisions", [])
            ],
            "session_diagnostics": result.get("session_diagnostics", {}),
            "data_usage_registered": registered,
            "total_return_qualified": False,
            "observed_at": datetime.now(UTC).isoformat(),
        }

    def _register(
        self, source: ArchiveSource, result: dict[str, Any], checksum: str
    ) -> bool:
        if not source.bars:
            raise ValueError("ARCHIVE_SOURCE_BARS_REQUIRED")
        for path in source.bars:
            _bytes(path)
            frame = pl.read_parquet(path)
            symbol = str(frame["symbol"][0])
            expected = (
                result["assets"][symbol]["dataset_hash"]
                if source.kind == "REFINEMENT"
                else result["dataset_hash"]
                if source.kind == "OPTIMIZATION"
                else next(iter(result["scenarios"].values()))["content_hash"]
            )
            if logical_content_hash(frame) != expected:
                raise ValueError("ARCHIVE_DATA_SOURCE_MISMATCH")
            register_research_use(
                frame,
                program="archive-view-" + source.id,
                reference_hash=checksum,
                registry_root=self.usage_root,
            )
        return True

    def artifact(self, id: str, name: str) -> dict[str, Any]:
        source = self._source(id)
        if source.kind == "CASH_SHARE":
            return self._cash(source).artifact(name)
        source, _, result, checksum = self._load(id)
        self._register(source, result, checksum)
        filenames = {
            "plan": "plan.json",
            "result": "result.json",
            "report": "report.md",
            "metrics": "metrics.csv",
        }
        if name not in filenames:
            raise ValueError("ARCHIVE_ARTIFACT_NOT_REGISTERED")
        raw = _bytes(source.root / filenames[name])
        return {
            "schema_version": "qf-archive-artifact-v1",
            "id": id,
            "artifact_id": name,
            "filename": filenames[name],
            "sha256": hashlib.sha256(raw).hexdigest(),
            "content": raw.decode("utf-8"),
            "live_authority": "NONE",
        }
