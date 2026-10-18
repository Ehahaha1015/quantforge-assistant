import json
import sys

from quantforge.autolab.adapters.codex import CodexAdapter
from quantforge.autolab.models import ReviewerKind, TransportStatus
from quantforge.autolab.review import parse_review


def test_malformed_review_is_schema_error() -> None:
    try:
        parse_review(
            json.dumps(
                {
                    "review_commit": "sha",
                    "raw_verdict": "approve",
                    "blocking_findings": [{}],
                }
            ),
            ReviewerKind.CODEX,
            "sha",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("malformed schema must be rejected")


def test_codex_adapter_maps_malformed_schema_to_transport() -> None:
    def runner(*args, **kwargs):
        from types import SimpleNamespace

        return SimpleNamespace(
            returncode=0,
            stdout='{"review_commit":"sha","raw_verdict":"approve","blocking_findings":[{}]}',
            stderr="",
        )

    review, attempt = CodexAdapter(executable=sys.executable, runner=runner).review(
        "prompt", ".", "sha", 1
    )
    assert review is None
    assert attempt.status == TransportStatus.SCHEMA_ERROR


def test_codex_adapter_distinguishes_parse_error() -> None:
    def runner(*args, **kwargs):
        from types import SimpleNamespace

        return SimpleNamespace(returncode=0, stdout="not-json", stderr="")

    review, attempt = CodexAdapter(executable=sys.executable, runner=runner).review(
        "prompt", ".", "sha", 1
    )
    assert review is None
    assert attempt.status == TransportStatus.PARSE_ERROR


def test_confirmation_requires_json_boolean() -> None:
    payload = json.dumps(
        {
            "review_commit": "sha",
            "raw_verdict": "approve",
            "blocking_findings": [
                {
                    "id": "b",
                    "classification": "CORRECTNESS_DEFECT",
                    "title": "bug",
                    "confirmed": "false",
                }
            ],
        }
    )
    try:
        parse_review(payload, ReviewerKind.CODEX, "sha")
    except ValueError:
        pass
    else:
        raise AssertionError("non-boolean confirmation must be rejected")


def test_merge_recommendation_requires_string() -> None:
    payload = json.dumps(
        {"review_commit": "sha", "raw_verdict": "approve", "merge_recommendation": {}}
    )
    try:
        parse_review(payload, ReviewerKind.CODEX, "sha")
    except ValueError:
        pass
    else:
        raise AssertionError("non-string recommendation must be rejected")
