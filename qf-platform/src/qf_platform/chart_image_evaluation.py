"""Offline scoring of operator-supplied, independently labelled image bundles."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .chart_reports import ChartReport, assess_alignment
from .storage import digest

FIELDS = (
    "recognized_symbol",
    "recognized_period",
    "recognized_timeframe",
    "recognized_adjustment",
    "image_quality",
)


class ImageCase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1, max_length=100)
    group_id: str = Field(min_length=1, max_length=100)
    split: Literal["DEVELOPMENT", "SEALED"]
    category: str = Field(min_length=1, max_length=100)
    image_path: str
    image_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    analysis_path: str
    analysis_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected: dict[str, str | None]
    expected_alignment: Literal["CONSISTENT", "MISMATCH", "UNCONFIRMED"]
    annotators: list[str] = Field(min_length=2, max_length=10)
    adjudication: str = Field(min_length=1, max_length=1000)


class ImageBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal["chart-image-labelled-bundle-v1"]
    provenance: Literal["DECLARED_INDEPENDENT_HUMAN_LABELS", "SYNTHETIC_FIXTURE"]
    protocol_id: str = Field(min_length=1, max_length=100)
    cases: list[ImageCase] = Field(min_length=1, max_length=10000)


def _read(root: Path, name: str, checksum: str) -> bytes:
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Bundle paths must stay relative to the bundle directory")
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Redirected bundle path")
    for item in (path, *path.parents):
        if item == root.parent:
            break
        if item.is_symlink() or item.is_junction():
            raise ValueError("Redirected bundle path")
    if path.stat().st_size > 8_000_000:
        raise ValueError("Bundle item too large")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != checksum:
        raise ValueError("Bundle item checksum mismatch")
    return raw


def _rate(correct: int, total: int) -> dict[str, Any]:
    if not total:
        return {"correct": correct, "total": total, "rate": None, "wilson_95": None}
    p, z = correct / total, 1.959963984540054
    denominator = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denominator
    margin = (
        z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    )
    return {
        "correct": correct,
        "total": total,
        "rate": p,
        "wilson_95": [max(0, center - margin), min(1, center + margin)],
    }


def evaluate_bundle(manifest: Path) -> dict[str, Any]:
    root = manifest.absolute().parent
    raw = manifest.read_bytes()
    if len(raw) > 8_000_000:
        raise ValueError("Manifest too large")
    bundle = ImageBundle.model_validate_json(raw)
    if len({c.id for c in bundle.cases}) != len(bundle.cases):
        raise ValueError("Duplicate case id")
    groups: dict[str, set[str]] = {}
    images: dict[str, set[str]] = {}
    rows = []
    for case in bundle.cases:
        groups.setdefault(case.group_id, set()).add(case.split)
        images.setdefault(case.image_sha256, set()).add(case.split)
        if len(set(case.annotators)) < 2 or any(not a.strip() for a in case.annotators):
            raise ValueError("Independent annotator identities required")
        if set(case.expected) != set(FIELDS):
            raise ValueError(
                "Labels must declare every extraction field, including null"
            )
        _read(root, case.image_path, case.image_sha256)
        saved = json.loads(_read(root, case.analysis_path, case.analysis_sha256))
        if saved.get("image", {}).get("sha256") != case.image_sha256:
            raise ValueError("Output belongs to a different image")
        if (
            bundle.provenance == "DECLARED_INDEPENDENT_HUMAN_LABELS"
            and ((saved.get("algorithm_analysis") or {}).get("source", {}) or {}).get(
                "provider"
            )
            == "OFFLINE_SYNTHETIC"
        ):
            raise ValueError(
                "Synthetic outputs cannot enter human-labelled real evaluation"
            )
        try:
            report = ChartReport.model_validate(saved.get("report"))
            alignment = assess_alignment(saved["request"], report)["status"]
            values = report.model_dump()
            schema_valid = True
        except (ValueError, TypeError, KeyError):
            values, alignment, schema_valid = {}, "INVALID", False
        rows.append(
            {
                "id": case.id,
                "group_id": case.group_id,
                "split": case.split,
                "category": case.category,
                "schema_valid": schema_valid,
                "image_sha256": case.image_sha256,
                "analysis_sha256": case.analysis_sha256,
                "fields_correct": {
                    field: schema_valid and values[field] == case.expected[field]
                    for field in FIELDS
                },
                "expected_alignment": case.expected_alignment,
                "alignment": alignment,
                "false_accept": alignment == "CONSISTENT"
                and case.expected_alignment != "CONSISTENT",
                "unnecessary_refusal": alignment != "CONSISTENT"
                and case.expected_alignment == "CONSISTENT",
            }
        )
    if any(len(splits) > 1 for splits in [*groups.values(), *images.values()]):
        raise ValueError("Group or duplicate image crosses development/sealed boundary")

    def summarize(selected: list[dict[str, Any]]) -> dict[str, Any]:
        negatives = [r for r in selected if r["expected_alignment"] != "CONSISTENT"]
        positives = [r for r in selected if r["expected_alignment"] == "CONSISTENT"]
        return {
            "cases": len(selected),
            "schema_success": _rate(
                sum(r["schema_valid"] for r in selected), len(selected)
            ),
            "fields": {
                f: _rate(sum(r["fields_correct"][f] for r in selected), len(selected))
                for f in FIELDS
            },
            "alignment_exact": _rate(
                sum(r["alignment"] == r["expected_alignment"] for r in selected),
                len(selected),
            ),
            "false_accepts": _rate(
                sum(r["false_accept"] for r in negatives), len(negatives)
            ),
            "unnecessary_refusals": _rate(
                sum(r["unnecessary_refusal"] for r in positives), len(positives)
            ),
        }

    return {
        "schema_version": "chart-image-quality-evaluation-v1",
        "evidence": bundle.provenance,
        "protocol_id": bundle.protocol_id,
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "dataset_identity": digest(bundle.model_dump()),
        "overall": summarize(rows),
        "by_split": {
            split: summarize([r for r in rows if r["split"] == split])
            for split in ("DEVELOPMENT", "SEALED")
        },
        "by_category": {
            category: summarize([r for r in rows if r["category"] == category])
            for category in sorted({r["category"] for r in rows})
        },
        "rows": rows,
        "real_model_calls": 0,
        "forecast_validated": False,
        "limitations": [
            "Labels and independent annotation are operator declarations; "
            "not independently attested here.",
            "Exact extraction and parser alignment only; "
            "no semantic-factuality or forecast metric.",
            "Wilson intervals describe case counts; "
            "related images/issuers remain correlated.",
            "No minimum sample size or release approval is inferred from these scores.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate_bundle(args.manifest)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "cases": result["overall"]["cases"],
                "real_model_calls": 0,
            }
        )
    )


if __name__ == "__main__":
    main()
