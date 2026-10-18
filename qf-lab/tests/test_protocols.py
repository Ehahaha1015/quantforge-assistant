from __future__ import annotations

import io
import json
import urllib.error
from typing import Any

import pytest

from qf_lab.gateway import GatewayError, ResearchDecision
from qf_lab.protocols import (
    ProtocolProvider,
    ProtocolSchemaError,
    output_schema,
    validate_endpoint,
)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://localhost./v1",
        "https://localalias/v1",
        "https://127.1/v1",
        "https://2130706433/v1",
        "https://0x7f000001/v1",
        "https://foo.local/v1",
    ],
)
def test_noncanonical_local_endpoints_deny(endpoint: str) -> None:
    with pytest.raises(GatewayError, match="MODEL_ENDPOINT_INVALID"):
        validate_endpoint(endpoint)


DECISION = {
    "action": "propose",
    "family": "moving_average_crossover",
    "short_window": 5,
    "long_window": 15,
    "rationale": "A bounded causal research hypothesis",
    "evidence_refs": [],
}


def test_post_response_revocation_is_a_precondition_not_schema_failure(
    monkeypatch: Any,
) -> None:
    calls = 0

    def secret() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise ValueError("offline-revoked-key-never-echo")
        return {"api_key": "offline-revocation-fixture"}

    class Opener:
        def open(self, request: Any, timeout: int) -> Any:
            response = io.BytesIO(b'{"data":[]}')
            response.geturl = lambda: request.full_url  # type: ignore[attr-defined]
            return response

    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener())
    provider = ProtocolProvider(
        "fixture",
        "OPENAI_COMPATIBLE",
        "https://api.example.com/v1",
        "fixture-model",
        secret=secret,
    )
    with pytest.raises(GatewayError, match="MODEL_BINDING_REVISION_REVOKED"):
        provider._request("/models", None)


@pytest.mark.parametrize(
    "protocol", ["OPENAI_COMPATIBLE", "ANTHROPIC_NATIVE", "GEMINI_NATIVE"]
)
def test_protocol_native_auth_structured_output_and_provenance(
    monkeypatch: Any, protocol: str
) -> None:
    requests = []
    key = "transient-fixture-secret"

    class Opener:
        def open(self, request: Any, timeout: int) -> Any:
            requests.append(request)
            assert key not in request.full_url
            assert key not in request.data.decode()
            text = json.dumps(DECISION)
            value = (
                {
                    "choices": [
                        {"finish_reason": "stop", "message": {"content": text}}
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 20},
                }
                if protocol == "OPENAI_COMPATIBLE"
                else {
                    "stop_reason": "end_turn",
                    "content": [{"type": "text", "text": text}],
                    "usage": {"input_tokens": 10, "output_tokens": 20},
                }
                if protocol == "ANTHROPIC_NATIVE"
                else {
                    "candidates": [
                        {"finishReason": "STOP", "content": {"parts": [{"text": text}]}}
                    ],
                    "usageMetadata": {
                        "promptTokenCount": 10,
                        "candidatesTokenCount": 20,
                    },
                }
            )
            response = io.BytesIO(json.dumps(value).encode())
            response.geturl = lambda: request.full_url  # type: ignore[attr-defined]
            return response

    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener())
    provider = ProtocolProvider(
        "profile-1",
        protocol,
        "https://api.example.com/v1",
        "test-model",
        secret=lambda: {"api_key": key},
    )
    decision, metadata = provider.complete_structured(
        {"phase": "first"}, ResearchDecision
    )
    assert decision.short_window == 5
    assert metadata["input_tokens"] == 10
    assert metadata["real_model_call"] is True
    assert key not in json.dumps(metadata)
    assert len(requests) == 1


def test_transport_never_logs_body_or_retries(monkeypatch: Any) -> None:
    count = 0

    class Opener:
        def open(self, request: Any, timeout: int) -> Any:
            nonlocal count
            count += 1
            raise urllib.error.URLError("transient-fixture-secret")

    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener())
    provider = ProtocolProvider(
        "p",
        "OPENAI_COMPATIBLE",
        "https://api.example.com",
        "m",
        secret=lambda: {"api_key": "transient-fixture-secret"},
    )
    with pytest.raises(GatewayError, match="MODEL_TRANSPORT_UNKNOWN") as failure:
        provider.test()
    assert "fixture-secret" not in str(failure.value)
    assert count == 1


def test_family_prompt_schema_matches_the_admission_union() -> None:
    from qf_lab.gateway import PARAMETER_FIELDS

    union = output_schema(ResearchDecision)["oneOf"]
    assert len(union) == len(PARAMETER_FIELDS)
    for option in union:
        family = option["properties"]["family"]["const"]
        assert (
            set(option["required"])
            == {"family", "action", "rationale", "evidence_refs"}
            | PARAMETER_FIELDS[family]
        )
        assert option["additionalProperties"] is False
        assert all(
            "anyOf" not in option["properties"][name]
            for name in PARAMETER_FIELDS[family]
        )


def test_schema_error_locations_never_echo_unrecognized_input(monkeypatch: Any) -> None:
    provider = ProtocolProvider(
        "p",
        "OPENAI_COMPATIBLE",
        "https://api.example.com",
        "m",
        secret=lambda: {"api_key": "transient-fixture-secret"},
    )
    from qf_lab.room_gateway import CriticDecision

    monkeypatch.setattr(
        provider,
        "_generate",
        lambda *args: (
            json.dumps({"transient-fixture-secret": "never-save"}),
            {"response_hash": "a" * 64, "actual_model": "m"},
        ),
    )
    with pytest.raises(ProtocolSchemaError) as caught:
        provider.complete_structured({}, CriticDecision)
    assert caught.value.schema_failure_locations
    assert ["UNRECOGNIZED_FIELD"] in caught.value.schema_failure_locations
    assert "fixture-secret" not in str(vars(caught.value))


@pytest.mark.parametrize("failure", ["REDIRECTED", "TOO_LARGE", "SECRET_ECHO_REJECTED"])
def test_protocol_guard_classifications_survive_schema_boundary(
    monkeypatch: Any, failure: str
) -> None:
    class Response:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def geturl(self) -> str:
            return (
                "https://untrusted.example"
                if failure == "REDIRECTED"
                else "https://api.example.com/test"
            )

        def read(self, size: int) -> bytes:
            return (
                b"x" * 2000001
                if failure == "TOO_LARGE"
                else b'{"fixture":"transient-fixture-secret"}'
            )

    class Opener:
        def open(self, request: Any, timeout: int) -> Any:
            return Response()

    monkeypatch.setattr("urllib.request.build_opener", lambda *args: Opener())
    provider = ProtocolProvider(
        "fixture",
        "OPENAI_COMPATIBLE",
        "https://api.example.com",
        "m",
        secret=lambda: {"api_key": "transient-fixture-secret"},
    )
    with pytest.raises(
        GatewayError,
        match="MODEL_"
        + ("RESPONSE_" if failure != "SECRET_ECHO_REJECTED" else "")
        + failure,
    ):
        provider._request("/test", None)
