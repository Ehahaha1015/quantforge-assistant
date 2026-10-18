import io
import json

import pytest
from test_personal_assistant import account, stream
from test_qq_bridge import event
from test_qq_group import group_bridge, group_message

from qf_platform import personal_provider
from qf_platform.personal_service import PersonalService
from qf_platform.storage import PlatformError


def completed(*, status="completed", annotations=None, tools=True):
    note = {
        "type": "url_citation",
        "title": "Official fixture",
        "url": "https://docs.python.org/3/",
    }
    output = (
        [{"type": "web_search_call", "id": "ws_fixture", "status": status}]
        if tools
        else []
    )
    output.append(
        {
            "type": "message",
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "text": "verified answer",
                    "annotations": [note] if annotations is None else annotations,
                }
            ],
        }
    )
    return {
        "type": "response.completed",
        "response": {
            "status": "completed",
            "output": output,
            "usage": {"total_tokens": 12},
        },
    }


@pytest.mark.parametrize("explicit", [False, True])
def test_web_request_only_enables_hosted_search_and_preserves_oauth_fields(
    tmp_path, monkeypatch, explicit
):
    store = account(tmp_path)
    store.write(
        "capabilities.json",
        {
            "web_search_enabled": True,
            "web_search_auto_enabled": True,
            "web_search_auto_model": "fixture-model",
        },
    )
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append(json.loads(request.data))
            assert request.get_header("Authorization") == "Bearer fixture-private-token"
            return io.BytesIO(stream(completed()).read())

    monkeypatch.setattr(personal_provider, "personal_opener", lambda: Opener())
    text, usage = personal_provider.answer(
        store,
        [
            {
                "role": "user",
                "content": "/联网 current fixture" if explicit else "current fixture",
            }
        ],
        "fixture-model",
        "fixture-private-token",
    )
    assert len(calls) == 1 and calls[0]["tools"] == [
        {"type": "web_search", "search_context_size": "low"}
    ]
    assert calls[0]["store"] is False and calls[0]["stream"] is True
    assert (calls[0].get("tool_choice") == "required") is explicit
    assert not {
        "max_output_tokens",
        "max_tool_calls",
        "previous_response_id",
        "conversation",
        "background",
    } & set(calls[0])
    assert "https://docs.python.org/3/" in text and usage["web_search_calls"] == 1


def test_search_requires_completed_tool_and_real_citations():
    delta = {
        "type": "response.output_text.delta",
        "delta": "answer\ue200cite\ue202fixture\ue201",
    }
    text, usage = personal_provider.parse_stream(
        stream(delta, completed()), allow_web=True, require_search=True
    )
    assert "https://docs.python.org/3/" in text and "\ue200" not in text
    assert usage["web_source_count"] == 1
    with pytest.raises(PlatformError, match="WEB_SEARCH_NOT_CONFIRMED"):
        personal_provider.parse_stream(
            stream(completed(tools=False)), allow_web=True, require_search=True
        )
    with pytest.raises(PlatformError, match="WEB_SOURCES_MISSING"):
        personal_provider.parse_stream(
            stream(completed(annotations=[])), allow_web=True, require_search=True
        )
    text, usage = personal_provider.parse_stream(
        stream(completed(tools=False)), allow_web=True
    )
    assert text == "verified answer" and usage["web_search_calls"] == 0


@pytest.mark.parametrize("annotation_event", [False, True])
def test_search_can_complete_through_output_item_events(annotation_event):
    final = completed()
    tool, message = final["response"]["output"]
    tool["id"] = "ws_stream"
    message["id"] = "msg_stream"
    annotations = message["content"][0]["annotations"]
    if annotation_event:
        message["content"][0]["annotations"] = []
    final["response"]["output"] = []
    events = [
        {"type": "response.output_item.done", "item": tool, "output_index": 0},
        {"type": "response.output_item.done", "item": message, "output_index": 1},
    ]
    if annotation_event:
        events.append(
            {
                "type": "response.output_text.annotation.added",
                "annotation": annotations[0],
            }
        )
    diagnostic = {}
    text, usage = personal_provider.parse_stream(
        stream(*events, final),
        allow_web=True,
        require_search=True,
        diagnostic=diagnostic,
    )
    assert "https://docs.python.org/3/" in text and usage["web_search_calls"] == 1
    assert diagnostic["web_statuses"] == ["completed"]


def test_streamed_forbidden_tool_is_rejected_even_if_final_output_omits_it():
    bad = {
        "type": "response.output_item.done",
        "item": {"id": "bad", "type": "image_generation_call", "status": "completed"},
    }
    with pytest.raises(PlatformError, match="RESPONSE_UNCONFIRMED"):
        personal_provider.parse_stream(stream(bad, completed()), allow_web=True)


def test_search_diagnostic_never_contains_response_text_or_source_urls():
    diagnostic = {}
    value = completed(tools=False)
    value["response"]["output"][0]["content"][0]["text"] = (
        "private-token private-question"
    )
    with pytest.raises(PlatformError, match="WEB_SEARCH_NOT_CONFIRMED"):
        personal_provider.parse_stream(
            stream(value), allow_web=True, require_search=True, diagnostic=diagnostic
        )
    rendered = json.dumps(diagnostic)
    assert "private" not in rendered and "https" not in rendered
    assert diagnostic["final_output_types"] == ["message"]


def test_unverified_model_keeps_ordinary_chat_working_without_enabling_search(
    tmp_path, monkeypatch
):
    store = account(tmp_path)
    store.write(
        "capabilities.json",
        {
            "web_search_enabled": True,
            "web_search_auto_enabled": True,
            "web_search_auto_model": "other-model",
        },
    )
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append(json.loads(request.data))
            return io.BytesIO(stream(completed(tools=False)).read())

    monkeypatch.setattr(personal_provider, "personal_opener", lambda: Opener())
    personal_provider.answer(
        store,
        [{"role": "user", "content": "hello"}],
        "fixture-model",
        "fixture-private-token",
    )
    assert calls[0]["tools"] == [] and "tool_choice" not in calls[0]


@pytest.mark.parametrize("status", ["in_progress", "failed", None])
def test_unfinished_search_never_returns_partial_answer(status):
    with pytest.raises(PlatformError, match="WEB_SEARCH_NOT_CONFIRMED"):
        personal_provider.parse_stream(stream(completed(status=status)), allow_web=True)


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "http://127.0.0.1/",
        "http://10.0.0.1/",
        "https://host.docker.internal/",
        "https://user:secret@example.com/",
        "https://example.com/\nsecret",
    ],
)
def test_citation_rendering_rejects_private_or_credential_urls_without_fetching(
    monkeypatch, url
):
    monkeypatch.setattr(
        personal_provider.urllib.request,
        "urlopen",
        lambda *args: pytest.fail("must not fetch citation URLs"),
    )
    value = completed(annotations=[{"type": "url_citation", "url": url}])
    with pytest.raises(PlatformError, match="WEB_SOURCES_MISSING"):
        personal_provider.parse_stream(stream(value), allow_web=True)


def test_web_flag_does_not_authorize_image_or_other_tools():
    value = completed()
    value["response"]["output"].insert(
        0, {"type": "image_generation_call", "status": "completed"}
    )
    with pytest.raises(PlatformError, match="RESPONSE_UNCONFIRMED"):
        personal_provider.parse_stream(stream(value), allow_web=True)
    with pytest.raises(PlatformError, match="RESPONSE_UNCONFIRMED"):
        personal_provider.parse_stream(stream(completed()))


def test_search_failure_keeps_machine_code_without_server_prose():
    value = {
        "type": "response.failed",
        "response": {
            "error": {
                "code": "subscription_sharing_unsupported_capability",
                "message": "private-token private-email",
            }
        },
    }
    with pytest.raises(PlatformError) as result:
        personal_provider.parse_stream(stream(value), allow_web=True)
    assert (
        str(result.value)
        == "PERSONAL_PLAN_REQUEST_FAILED_SUBSCRIPTION_SHARING_UNSUPPORTED_CAPABILITY"
    )
    assert "private" not in str(result.value)


def test_disabled_web_does_not_reserve_model_request(tmp_path):
    service = PersonalService(tmp_path, account(tmp_path))
    with pytest.raises(PlatformError, match="PERSONAL_WEB_DISABLED"):
        service.queue("alice", "disabled", "/联网 query")
    with service.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM request_journal").fetchone()[0] == 0


def test_search_disabled_after_queue_is_stopped_before_request_reservation(
    tmp_path, monkeypatch
):
    store = account(tmp_path)
    store.write("capabilities.json", {"web_search_enabled": True})
    service = PersonalService(tmp_path, store)
    service.queue("alice", "queued", "/联网 query")
    store.write("capabilities.json", {"web_search_enabled": False})
    monkeypatch.setattr(
        personal_provider,
        "answer",
        lambda *args: pytest.fail("no network call allowed"),
    )
    assert service.tick() and service.results()[0]["error"] == "PERSONAL_WEB_DISABLED"
    assert service.status("alice")["account_requests_today"] == 0


def test_group_commands_report_image_limit_without_calls_and_route_search(
    tmp_path, monkeypatch
):
    app, _ = group_bridge(tmp_path)
    for number, command in enumerate(
        ("/生图 cat", "/画图 cat", "/image cat", "/能力", "/联网", "/联网 fixture"), 1
    ):
        assert app.accept(group_message(number, command)) and app.process_one()
    assert not app.personal.tick()
    assert app.accept(event(10, "/生图 cat")) and app.process_one()
    assert not app.personal.tick()
    app.personal.account.write("capabilities.json", {"web_search_enabled": True})
    calls = []

    def answer(account, messages, model, token):
        calls.append(messages[-1]["content"])
        return "fixture result", {"web_search_calls": 1}

    monkeypatch.setattr(personal_provider, "answer", answer)
    assert app.accept(group_message(20, "/联网 query")) and app.process_one()
    assert app.personal.tick() and calls == ["/联网 query"]
    assert app.personal.status("group:987654321:3140000001")["web_search_enabled"]
    assert not app.personal.status("3140000001")["image_generation_supported"]
    assert app.charts.status()["calls_used"] == 0
