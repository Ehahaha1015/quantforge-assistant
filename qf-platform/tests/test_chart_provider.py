import base64
import json
import urllib.error
from typing import Any

import pytest

from qf_platform import chart_provider
from qf_platform.storage import PlatformError


def response_payload() -> dict[str, Any]:
    report = {
        "image_quality": "UNREADABLE",
        "recognized_symbol": None,
        "recognized_period": None,
        "summary": "软件夹具图片不可辨认。",
        "observed_trend": "UNCLEAR",
        "outlook": "UNCLEAR",
        "observations": [],
        "support_zones": [],
        "resistance_zones": [],
        "volume_comment": "无法辨认成交量。",
        "scenarios": [],
        "uncertainties": ["这是软件夹具，不是市场或模型效果证据。"],
    }
    return {
        "choices": [
            {"finish_reason": "stop", "message": {"content": json.dumps(report)}}
        ],
        "model": "deepseek-flash",
        "usage": {"prompt_tokens": 100, "completion_tokens": 200},
    }


def test_documented_inline_image_request_and_structured_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class Response:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def read(self, limit: int) -> bytes:
            assert limit == 256_001
            return json.dumps(response_payload()).encode()

    class Opener:
        def open(self, request: Any, timeout: int) -> Response:
            captured.update(json.loads(request.data))
            assert request.full_url == "https://api.deepseek.com/chat/completions"
            assert request.get_header("Authorization") == "Bearer fixture-only-key"
            assert timeout == 90
            return Response()

    monkeypatch.setattr(
        chart_provider.urllib.request, "build_opener", lambda *args: Opener()
    )
    image = b"fixture image bytes"
    report, usage = chart_provider.analyze(
        "fixture-only-key",
        image,
        "image/png",
        {"symbol": "TEST"},
        {"status": "UNAVAILABLE", "reason": "软件夹具，无行情"},
    )
    assert captured["model"] == "deepseek-flash" and captured["thinking"] == {
        "type": "disabled"
    }
    assert captured["response_format"] == {"type": "json_object"}
    text = json.loads(captured["messages"][1]["content"][0]["text"])
    assert text["deterministic_core_analysis"] == {
        "status": "UNAVAILABLE",
        "reason": "软件夹具，无行情",
    }
    image_block = captured["messages"][1]["content"][1]
    assert image_block == {
        "type": "image_url",
        "image_url": {
            "url": "data:image/png;base64," + base64.b64encode(image).decode(),
            "detail": "original",
        },
    }
    assert report.outlook == "UNCLEAR" and usage["prompt_tokens"] == 100
    assert len(usage["response_sha256"]) == 64


def test_timeout_is_unknown_without_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    class Opener:
        def open(self, *args: Any, **kwargs: Any) -> Any:
            calls.append(1)
            raise urllib.error.URLError("fixture transport ambiguity")

    monkeypatch.setattr(
        chart_provider.urllib.request, "build_opener", lambda *args: Opener()
    )
    with pytest.raises(chart_provider.UnknownChartCall):
        chart_provider.analyze("fixture-only-key", b"fixture", "image/png", {})
    assert calls == [1]


@pytest.mark.parametrize("kind", ["schema", "truncated", "oversize"])
def test_invalid_or_incomplete_responses_are_not_reports(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    class Response:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def read(self, limit: int) -> bytes:
            value = response_payload()
            if kind == "oversize":
                return b"x" * limit
            if kind == "truncated":
                value["choices"][0]["finish_reason"] = "length"
            else:
                value["choices"][0]["message"]["content"] = '{"win_probability":1}'
            return json.dumps(value).encode()

    class Opener:
        def open(self, *args: Any, **kwargs: Any) -> Response:
            return Response()

    monkeypatch.setattr(
        chart_provider.urllib.request, "build_opener", lambda *args: Opener()
    )
    with pytest.raises(PlatformError):
        chart_provider.analyze("fixture-only-key", b"fixture", "image/png", {})


@pytest.mark.parametrize(
    "kind,reason",
    [
        ("length", "OUTPUT_TRUNCATED"),
        ("empty", "EMPTY_CONTENT"),
        ("bad_json", "CONTENT_JSON_INVALID"),
        ("bad_field", "SCHEMA_INVALID"),
        ("bad_envelope", "ENVELOPE_INVALID"),
        ("invalid_finish", "FINISH_REASON_INVALID"),
    ],
)
def test_failed_response_preserves_bytes_and_safe_reason_without_network(kind, reason):
    value = response_payload()
    choice = value["choices"][0]
    if kind == "length":
        choice["finish_reason"] = "length"
    elif kind == "empty":
        choice["message"]["content"] = ""
    elif kind == "bad_json":
        choice["message"]["content"] = '{"summary":'
    elif kind == "bad_field":
        report = json.loads(choice["message"]["content"])
        report["summary"] = {"secret-value-must-remain-private": 1}
        choice["message"]["content"] = json.dumps(report)
    elif kind == "bad_envelope":
        value["choices"] = []
    else:
        choice["finish_reason"] = {"bad": "field"}
    raw = json.dumps(value).encode()
    with pytest.raises(chart_provider.ProviderFailure) as error:
        chart_provider.parse_response(raw)
    failure = error.value
    assert failure.raw_response == raw and failure.diagnostics["reason"] == reason
    assert "secret-value-must-remain-private" not in json.dumps(failure.diagnostics)
    if kind == "bad_field":
        assert failure.diagnostics["validation_errors"][0]["field"] == "summary"


def test_valid_response_parsing_still_enforces_existing_unreadable_safety_gate():
    value = response_payload()
    report = json.loads(value["choices"][0]["message"]["content"])
    report["outlook"] = "BULLISH"
    value["choices"][0]["message"]["content"] = json.dumps(report)
    with pytest.raises(chart_provider.ProviderFailure) as error:
        chart_provider.parse_response(json.dumps(value).encode())
    assert error.value.diagnostics["reason"] == "SCHEMA_INVALID"
