import base64
import hashlib
import json
from pathlib import Path

import pytest

from qf_platform.chart_demo import sample_report, sample_request
from qf_platform.chart_image_evaluation import FIELDS, evaluate_bundle


def bundle(tmp_path: Path, *, invalid_output: bool = False) -> Path:
    request = sample_request()
    image = base64.b64decode(request.pop("image_base64"))
    (tmp_path / "image.png").write_bytes(image)
    report = sample_report(request).model_dump()
    saved = {
        "image": {"sha256": hashlib.sha256(image).hexdigest()},
        "request": request,
        "report": None if invalid_output else report,
        "algorithm_analysis": {"source": {"provider": "OFFLINE_SYNTHETIC"}},
    }
    raw = json.dumps(saved).encode()
    (tmp_path / "analysis.json").write_bytes(raw)
    manifest = {
        "schema_version": "chart-image-labelled-bundle-v1",
        "provenance": "SYNTHETIC_FIXTURE",
        "protocol_id": "fixture-only",
        "cases": [
            {
                "id": "one",
                "group_id": "same-chart",
                "split": "SEALED",
                "category": "synthetic-readable",
                "image_path": "image.png",
                "image_sha256": saved["image"]["sha256"],
                "analysis_path": "analysis.json",
                "analysis_sha256": hashlib.sha256(raw).hexdigest(),
                "expected": {field: report[field] for field in FIELDS},
                "expected_alignment": "CONSISTENT",
                "annotators": ["fixture-a", "fixture-b"],
                "adjudication": "Synthetic labels only; no real accuracy evidence.",
            }
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_scores_exact_fields_and_keeps_synthetic_scope_explicit(tmp_path):
    result = evaluate_bundle(bundle(tmp_path))
    assert result["evidence"] == "SYNTHETIC_FIXTURE"
    assert result["overall"]["schema_success"]["rate"] == 1
    assert all(v["rate"] == 1 for v in result["overall"]["fields"].values())
    assert result["overall"]["unnecessary_refusals"]["rate"] == 0
    assert result["overall"]["false_accepts"]["rate"] is None
    assert result["overall"]["schema_success"]["wilson_95"][0] < 1
    assert result["real_model_calls"] == 0 and not result["forecast_validated"]


def test_invalid_output_counts_as_failure_instead_of_disappearing(tmp_path):
    result = evaluate_bundle(bundle(tmp_path, invalid_output=True))
    assert result["overall"]["cases"] == 1
    assert result["overall"]["schema_success"]["rate"] == 0
    assert result["overall"]["unnecessary_refusals"]["rate"] == 1


@pytest.mark.parametrize(
    "change",
    [
        "cross-split",
        "duplicate-id",
        "human-claim",
        "missing-label",
        "path-escape",
        "same-annotator",
    ],
)
def test_rejects_contaminated_or_incomplete_bundles(tmp_path, change):
    path = bundle(tmp_path)
    value = json.loads(path.read_bytes())
    case = value["cases"][0]
    if change == "cross-split":
        value["cases"].append({**case, "id": "two", "split": "DEVELOPMENT"})
    elif change == "duplicate-id":
        value["cases"].append(case)
    elif change == "human-claim":
        value["provenance"] = "DECLARED_INDEPENDENT_HUMAN_LABELS"
    elif change == "missing-label":
        del case["expected"]["recognized_period"]
    elif change == "path-escape":
        case["analysis_path"] = "../private.json"
    else:
        case["annotators"] = ["same", "same"]
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        evaluate_bundle(path)


def test_original_bytes_and_output_image_binding_are_verified(tmp_path):
    path = bundle(tmp_path)
    (tmp_path / "image.png").write_bytes(b"corrupt image")
    with pytest.raises(ValueError, match="checksum"):
        evaluate_bundle(path)
