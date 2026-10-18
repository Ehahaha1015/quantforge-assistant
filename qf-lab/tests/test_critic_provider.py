from __future__ import annotations

import json
import sqlite3
import urllib.error
from pathlib import Path
from typing import Any, Self

import pytest

from qf_lab.cli import main
from qf_lab.gateway import (
    DeepSeekProvider,
    FakeProvider,
    GatewayError,
    ModelGateway,
    OpenAIProvider,
    _NoRedirect,
    _parse_decision,
)


class Response:
    def __init__(self, value: Any, url: str = OpenAIProvider.endpoint) -> None:
        self.raw = json.dumps(value).encode()
        self.url = url

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def geturl(self) -> str:
        return self.url

    def read(self, size: int) -> bytes:
        return self.raw[:size]


def envelope(content: str, **message: Any) -> dict[str, Any]:
    return {
        "id": "response-fixture",
        "model": "gpt-4.1-mini",
        "usage": {"total_tokens": 50},
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content, **message},
            }
        ],
    }


def context() -> dict[str, Any]:
    return {"phase": "first", "max_long_window": 20, "evidence": []}


def test_missing_critic_credential_blocks_cli_before_primary_calls_or_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        "sys.argv",
        [
            "qf-lab",
            "--core-root",
            str(tmp_path / "core"),
            "--lab-root",
            str(tmp_path / "lab"),
            "research",
            "bounded goal",
            "--dataset",
            "fixture",
            "--program",
            "fixture",
            "--provider",
            "fake",
            "--critic-provider",
            "openai",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "BLOCKED" and "OPENAI_API_KEY" in result["reason"]
    assert not (tmp_path / "core").exists() and not (tmp_path / "lab").exists()


def test_critic_shares_durable_gateway_budget_and_records_provider_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sentinel = "secret-transport-fixture-do-not-persist"
    monkeypatch.setenv("OPENAI_API_KEY", sentinel)
    decision, _ = FakeProvider().complete(context())

    class Opener:
        def open(self, request: Any, *, timeout: int) -> Response:
            assert request.full_url == "https://api.openai.com/v1/chat/completions"
            assert request.get_header("Authorization") == "Bearer " + sentinel
            assert timeout == 60
            body = json.loads(request.data)
            assert body["model"] == "gpt-4.1-mini" and body["stream"] is False
            assert "tools" not in body and "thinking" not in body
            return Response(envelope(decision.model_dump_json()))

    def build(*handlers: Any) -> Opener:
        assert any(isinstance(item, _NoRedirect) for item in handlers)
        return Opener()

    monkeypatch.setattr("urllib.request.build_opener", build)
    path = tmp_path / "calls.sqlite"
    gateway = ModelGateway(path, FakeProvider(), max_calls=3)
    try:
        gateway.call(context())
        gateway.call(context())
        critic = OpenAIProvider()
        gateway.call(context(), provider=critic)
        with pytest.raises(GatewayError, match="BUDGET"):
            gateway.call(context(), provider=critic)
        requests = [row for row in gateway.ledger() if row["phase"] == "REQUEST"]
        assert [row["provider"] for row in requests] == [
            "deterministic-fake",
            "deterministic-fake",
            "openai-official",
        ]
        assert sentinel not in json.dumps(gateway.ledger())
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            gateway.connection.execute("UPDATE calls SET phase='tamper'")
    finally:
        gateway.close()
    reopened = ModelGateway(path, FakeProvider(), max_calls=3)
    try:
        with pytest.raises(GatewayError, match="BUDGET"):
            reopened.call(context(), provider=OpenAIProvider())
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "attack", ["tool", "refusal", "redirect", "http", "transport", "multi", "duplicate"]
)
def test_critic_provider_fails_closed_without_leaking_credentials(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    attack: str,
) -> None:
    sentinel = "secret-response-fixture-do-not-persist"
    monkeypatch.setenv("OPENAI_API_KEY", sentinel)
    decision, _ = FakeProvider().complete(context())
    content = decision.model_dump_json()

    class Opener:
        def open(self, request: Any, *, timeout: int) -> Response:
            if attack == "http":
                raise urllib.error.HTTPError(request.full_url, 401, sentinel, {}, None)
            if attack == "transport":
                raise urllib.error.URLError(sentinel)
            if attack == "redirect":
                return Response(envelope(content), "https://unsafe.invalid/path")
            if attack == "tool":
                return Response(
                    envelope(content, tool_calls=[{"function": {"name": "execute"}}])
                )
            if attack == "refusal":
                return Response(envelope(content, refusal="refusal fixture"))
            if attack == "multi":
                return Response(envelope(content + content))
            return Response(
                envelope(content[:-1] + ',"family":"moving_average_crossover"}')
            )

    monkeypatch.setattr("urllib.request.build_opener", lambda *handlers: Opener())
    gateway = ModelGateway(tmp_path / "calls.sqlite", FakeProvider(), max_calls=3)
    try:
        with pytest.raises(GatewayError) as exc:
            gateway.call(context(), provider=OpenAIProvider())
        assert sentinel not in str(exc.value)
        assert sentinel not in json.dumps(gateway.ledger())
        assert [row["phase"] for row in gateway.ledger()] == ["REQUEST", "ERROR"]
    finally:
        gateway.close()


def test_no_redirect_handler_refuses_before_forwarding_authorization() -> None:
    assert (
        _NoRedirect().redirect_request(
            None, None, 302, "fixture", {}, "https://unsafe.invalid"
        )
        is None
    )  # type: ignore[arg-type]


@pytest.mark.parametrize("model", ["", "bad/model", "bad\nmodel", "x" * 81])
def test_provider_model_configuration_is_finite_and_scrubbed(model: str) -> None:
    with pytest.raises(GatewayError, match="CONFIGURATION"):
        DeepSeekProvider(model)


@pytest.mark.parametrize(
    "raw", ["[]", "{}{}", "```json\n{}\n```", '{"action":"propose","action":"freeze"}']
)
def test_provider_parser_rejects_non_single_or_duplicate_object(raw: str) -> None:
    with pytest.raises((GatewayError, ValueError)):
        _parse_decision(raw)
