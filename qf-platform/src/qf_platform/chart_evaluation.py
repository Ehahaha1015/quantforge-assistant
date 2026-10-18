"""Offline output-contract evaluation, separate from model accuracy."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .chart_demo import sample_report, sample_request
from .chart_provider import MODEL, ProviderFailure, parse_response
from .chart_reports import STRATEGY_VERSION, ChartReport, assess_alignment, instructions
from .storage import canonical, digest


def envelope(report: dict[str, Any], **changes: Any) -> bytes:
    return canonical(
        {
            "model": "OFFLINE_FIXTURE",
            "choices": [
                {"finish_reason": "stop", "message": {"content": canonical(report)}}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
            **changes,
        }
    ).encode()


def cases() -> list[dict[str, Any]]:
    metadata = sample_request()
    report = sample_report(metadata).model_dump(mode="json")
    rows: list[dict[str, Any]] = []

    def add(name: str, raw: bytes, expected: str) -> None:
        rows.append(
            {"id": name, "raw": raw, "expected": expected, "metadata": metadata}
        )

    add("aligned", envelope(report), "READY")
    refusal_changes: dict[str, dict[str, Any]] = {
        "wrong-symbol": {"recognized_symbol": "000001"},
        "wrong-period": {"recognized_period": "2025-01-01 至 2025-01-31"},
        "wrong-timeframe": {"recognized_timeframe": "1w"},
        "wrong-adjustment": {"recognized_adjustment": "QFQ"},
        "unknown-adjustment": {"recognized_adjustment": "UNKNOWN"},
        "unknown-symbol": {"recognized_symbol": None},
        "unknown-timeframe": {"recognized_timeframe": None},
        "unknown-period": {"recognized_period": None},
    }
    for name, changes in refusal_changes.items():
        add(name, envelope({**report, **changes}), "REFUSED")
    unreadable = sample_report(metadata, insufficient=True).model_dump(mode="json")
    add("unreadable", envelope(unreadable), "REFUSED")
    add(
        "not-chart", envelope({**unreadable, "image_quality": "NOT_A_CHART"}), "REFUSED"
    )
    for field in ("summary", "observations", "scenarios", "uncertainties"):
        broken = deepcopy(report)
        del broken[field]
        add("missing-" + field, envelope(broken), "INVALID")
    for name, changes in {
        "no-observations": {"observations": []},
        "one-scenario": {"scenarios": report["scenarios"][:1]},
        "extra-field": {"fabricated_probability": 0.99},
        "numeric-summary": {"summary": 100},
        "unreadable-outlook": {"image_quality": "UNREADABLE", "outlook": "BULLISH"},
    }.items():
        add(name, envelope({**report, **changes}), "INVALID")
    add("invalid-envelope-json", b"{broken", "INVALID")
    add("missing-envelope", b"{}", "INVALID")
    add(
        "truncated",
        envelope(
            report,
            choices=[
                {"finish_reason": "length", "message": {"content": canonical(report)}}
            ],
        ),
        "INVALID",
    )
    add(
        "invalid-content-json",
        envelope(
            report,
            choices=[{"finish_reason": "stop", "message": {"content": "broken"}}],
        ),
        "INVALID",
    )
    add(
        "negative-token-usage", envelope(report, usage={"prompt_tokens": -1}), "INVALID"
    )
    return rows


def evaluate() -> dict[str, Any]:
    rows = []
    for case in cases():
        alignment = None
        try:
            report, _ = parse_response(case["raw"])
            alignment = assess_alignment(case["metadata"], report)["status"]
            actual = "READY" if alignment == "CONSISTENT" else "REFUSED"
            reason = None
        except ProviderFailure as error:
            actual, reason = "INVALID", error.diagnostics["reason"]
        rows.append(
            {
                "id": case["id"],
                "expected": case["expected"],
                "actual": actual,
                "alignment": alignment,
                "reason": reason,
                "passed": actual == case["expected"],
                "input_sha256": hashlib.sha256(case["raw"]).hexdigest(),
            }
        )
    refused = [r for r in rows if r["expected"] != "READY"]
    return {
        "schema_version": "chart-contract-evaluation-v1",
        "at": datetime.now(UTC).isoformat(),
        "evidence": "SYNTHETIC_OUTPUT_CONTRACTS_NOT_MODEL_QUALITY",
        "dataset_sha256": digest(
            [
                {k: v for k, v in c.items() if k != "raw"}
                | {"raw_sha256": hashlib.sha256(c["raw"]).hexdigest()}
                for c in cases()
            ]
        ),
        "prompt_sha256": hashlib.sha256(instructions().encode()).hexdigest(),
        "schema_sha256": digest(ChartReport.model_json_schema()),
        "strategy_version": STRATEGY_VERSION,
        "configured_live_model": MODEL,
        "parser_source_sha256": hashlib.sha256(
            Path(__file__).with_name("chart_provider.py").read_bytes()
        ).hexdigest(),
        "cases": len(rows),
        "passed": all(r["passed"] for r in rows),
        "false_accepts": sum(r["actual"] == "READY" for r in refused),
        "refusal_recall": sum(r["actual"] != "READY" for r in refused) / len(refused),
        "failure_types": dict(Counter(r["reason"] for r in rows if r["reason"])),
        "rows": rows,
        "real_model_calls": 0,
        "limitations": [
            "手工声明的合成输出预期，不测量真实识图质量、自然语言事实性或预测准确率。",
            "未使用这些样本调优提示词；未来修改需保留本次基线。",
        ],
    }


def audit_saved(root: Path) -> dict[str, Any]:
    """Audit saved outputs without independent image ground truth."""
    from .chart_analysis import ChartAnalysisService

    app = ChartAnalysisService(root)
    rows = []
    for item in app.list()["items"]:
        value = app.get(item["id"])
        if value.get("report"):
            report = ChartReport.model_validate(value["report"])
            rows.append(
                {
                    "job_id": item["id"],
                    "report_sha256": digest(value["report"]),
                    "schema_valid": True,
                    "metadata_alignment": assess_alignment(value["request"], report)[
                        "status"
                    ],
                }
            )
    return {
        "schema_version": "saved-chart-output-audit-v1",
        "evidence": "EXISTING_MODEL_OUTPUTS_WITHOUT_INDEPENDENT_IMAGE_LABELS",
        "rows": rows,
        "count": len(rows),
        "real_model_calls": 0,
        "measures_model_accuracy": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--saved-root", type=Path)
    args = parser.parse_args()
    result = audit_saved(args.saved_root) if args.saved_root else evaluate()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(
        canonical(
            {
                "output": str(args.output),
                "passed": result.get("passed"),
                "count": result.get("cases", result.get("count")),
            }
        )
    )
    if result.get("passed") is False:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
