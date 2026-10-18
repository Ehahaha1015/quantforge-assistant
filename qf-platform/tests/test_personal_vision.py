import base64
import io
import json
import sqlite3
from contextlib import closing
from decimal import Decimal

import pytest
from test_chart_analysis import configure, image_payload
from test_personal_assistant import account
from test_qq_bridge import USER, event
from test_qq_group import OTHER, group_bridge, group_message

from qf_platform import personal_provider, personal_vision
from qf_platform.chart_analysis import ChartAnalysisService
from qf_platform.personal_service import PersonalService
from qf_platform.storage import PlatformError


def image_bytes():
    return base64.b64decode(image_payload()["image_base64"])


def service(tmp_path, monkeypatch):
    app = PersonalService(tmp_path, account(tmp_path))
    app.vision.configure("10.00")
    monkeypatch.setattr(personal_vision, "api_key", lambda root: "fixture-deepseek-key")
    return app


def describe_fixture(*args):
    return "[DeepSeek识图] fixture image observation", {
        "provider": "DeepSeek",
        "input_tokens": 10,
        "output_tokens": 20,
        "total_tokens": 30,
    }


def test_provider_uses_current_image_and_question_only(tmp_path, monkeypatch):
    captured = []

    class Opener:
        def open(self, request, timeout):
            captured.append(json.loads(request.data))
            assert request.full_url == "https://api.deepseek.com/chat/completions"
            assert request.get_header("Authorization") == "Bearer fixture-key"
            return io.BytesIO(
                json.dumps(
                    {
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {"content": "fixture answer"},
                            }
                        ],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
                    }
                ).encode()
            )

    monkeypatch.setattr(
        personal_vision.urllib.request, "build_opener", lambda *args: Opener()
    )
    answer, usage = personal_vision.describe(
        "fixture-key", image_bytes(), "image/png", "current question"
    )
    assert answer.startswith("[DeepSeek") and usage["provider"] == "DeepSeek"
    assert len(captured) == 1 and captured[0]["model"] == "deepseek-flash"
    assert captured[0]["max_tokens"] == 3000 and "tools" not in captured[0]
    assert len(captured[0]["messages"]) == 2
    blocks = captured[0]["messages"][1]["content"]
    assert blocks[0]["text"] == "current question" and blocks[1]["image_url"][
        "url"
    ].startswith("data:image/png;base64,")


def test_key_reuse_is_read_only_and_stock_budget_stays_separate(tmp_path):
    charts = ChartAnalysisService(tmp_path)
    configure(charts)
    before = charts.status()
    with charts.connect() as db:
        journal = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert personal_vision.api_key(tmp_path) == "fixture-only-not-a-real-api-key"
    vision = personal_vision.VisionBudget(tmp_path)
    vision.configure("10.00")
    call = vision.reserve("vision-key", "question", {"sha256": "a" * 64})
    vision.finish("vision-key", "FAILED")
    assert Decimal(call["reserved_cny"]) > 0
    assert charts.status() == before
    with charts.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == journal
    assert vision.ledger.path != tmp_path / "platform.sqlite"


def test_vision_works_after_chatgpt_daily_limit_and_does_not_forward_history(
    tmp_path, monkeypatch
):
    app = service(tmp_path, monkeypatch)
    monkeypatch.setattr(
        personal_provider, "answer", lambda *args: ("PRIVATE_TEXT_CONTEXT", {})
    )
    for number in range(20):
        app.queue(USER, "text-" + str(number), "text question")
        assert app.tick()
    captures = []

    def describe(key, data, mime, question):
        captures.append((data, question))
        return describe_fixture()

    monkeypatch.setattr(personal_vision, "describe", describe)
    app.queue(USER, "image", "only image question", image=image_bytes())
    assert app.tick()
    assert captures == [(image_bytes(), "only image question")]
    assert app.status(USER)["account_requests_today"] == 20
    assert app.status(USER)["vision"]["requests_today"] == 1
    assert app.results()[-1]["status"] == "COMPLETED"


def test_vision_result_becomes_text_context_without_original_image(
    tmp_path, monkeypatch
):
    app = service(tmp_path, monkeypatch)
    monkeypatch.setattr(personal_vision, "describe", describe_fixture)
    app.queue(USER, "picture", "question", image=image_bytes())
    app.tick()
    captures = []

    def text_answer(account, messages, model, token):
        captures.extend(messages)
        return "followup", {}

    monkeypatch.setattr(personal_provider, "answer", text_answer)
    app.queue(USER, "followup", "explain previous observation")
    app.tick()
    assert any("[DeepSeek" in x["content"] for x in captures)
    assert all(
        isinstance(x["content"], str) and "base64" not in x["content"] for x in captures
    )
    assert app.vision.status()["calls_used"] == 1
    assert app.status(USER)["account_requests_today"] == 1


def test_unknown_vision_call_retains_reserve_and_never_replays(tmp_path, monkeypatch):
    app = service(tmp_path, monkeypatch)
    calls = []

    def unknown(*args):
        calls.append(1)
        raise PlatformError("PERSONAL_VISION_EFFECT_UNKNOWN")

    monkeypatch.setattr(personal_vision, "describe", unknown)
    app.queue(USER, "unknown", "question", image=image_bytes())
    assert app.tick() and not app.tick()
    assert app.results()[0]["status"] == "UNKNOWN" and calls == [1]
    before = app.vision.status()
    assert before["calls_used"] == 1 and Decimal(before["reserved_cny"]) > 0
    assert app.queue(USER, "unknown", "question", image=image_bytes()) == "unknown"
    assert not app.tick() and calls == [1]
    with pytest.raises(PlatformError, match="BELOW_HISTORY"):
        app.vision.configure("0.00")
    assert app.vision.status() == before


def test_reserved_but_unconfirmed_job_is_not_sent_again(tmp_path, monkeypatch):
    app = service(tmp_path, monkeypatch)
    app.queue(USER, "interrupted", "question", image=image_bytes())
    with app.connect() as db:
        metadata = json.loads(
            db.execute("SELECT payload FROM turn_images").fetchone()[0]
        )
    app.vision.reserve("interrupted", "question", metadata)
    monkeypatch.setattr(
        personal_vision, "describe", lambda *args: pytest.fail("must not resend")
    )
    assert app.tick()
    assert app.results()[0]["error"] == "PERSONAL_VISION_RESERVATION_NOT_REPLAYABLE"
    assert app.vision.status()["calls_used"] == 1


def test_image_tampering_is_refused_before_reservation(tmp_path, monkeypatch):
    app = service(tmp_path, monkeypatch)
    app.queue(USER, "tamper", "question", image=image_bytes())
    next(app.images.iterdir()).write_bytes(b"changed bytes")
    monkeypatch.setattr(
        personal_vision, "describe", lambda *args: pytest.fail("bad image")
    )
    app.tick()
    assert app.results()[0]["error"] == "PERSONAL_VISION_IMAGE_INTEGRITY_FAILED"
    assert app.vision.status()["calls_used"] == 0


def test_image_metadata_is_immutable_and_idempotency_checks_the_image(
    tmp_path, monkeypatch
):
    app = service(tmp_path, monkeypatch)
    app.queue(USER, "same", "question", image=image_bytes())
    assert app.queue(USER, "same", "question", image=image_bytes()) == "same"
    with pytest.raises(PlatformError, match="IDEMPOTENCY_CONFLICT"):
        app.queue(USER, "same", "question")
    with closing(app.connect()) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE turn_images SET payload='{}'")


def test_vision_budget_refuses_before_send_without_consuming_text_quota(
    tmp_path, monkeypatch
):
    app = service(tmp_path, monkeypatch)
    app.vision.configure("0.01")
    monkeypatch.setattr(
        personal_vision, "describe", lambda *args: pytest.fail("budget unavailable")
    )
    app.queue(USER, "budget", "question", image=image_bytes())
    app.tick()
    assert app.results()[0]["error"] == "PERSONAL_VISION_BUDGET_EXHAUSTED"
    assert app.status(USER)["account_requests_today"] == 0
    assert app.vision.status()["calls_used"] == 0


def test_vision_daily_limit_is_shared_and_separate_from_chatgpt(tmp_path, monkeypatch):
    app = service(tmp_path, monkeypatch)
    monkeypatch.setattr(personal_vision, "describe", describe_fixture)
    for number in range(21):
        app.queue(
            USER if number % 2 else OTHER,
            "image-" + str(number),
            "question",
            image=image_bytes(),
        )
        app.tick()
    assert app.vision.status()["requests_today"] == 20
    assert app.results()[-1]["error"] == "PERSONAL_VISION_DAILY_LIMIT"
    assert app.status(USER)["account_requests_today"] == 0


def test_vision_does_not_depend_on_chatgpt_token_refresh(tmp_path, monkeypatch):
    app = service(tmp_path, monkeypatch)
    value = app.account.read("credentials.enc")
    value.update(expires_at=0, refresh_token=None)
    app.account.write("credentials.enc", value)
    monkeypatch.setattr(personal_vision, "describe", describe_fixture)
    app.queue(USER, "offline-gpt", "question", image=image_bytes())
    assert app.tick() and app.results()[0]["status"] == "COMPLETED"
    with pytest.raises(PlatformError, match="AUTH_REQUIRED"):
        app.queue(USER, "text", "question")


def test_group_image_and_caption_routes_to_vision_not_chart_or_chatgpt(
    tmp_path, monkeypatch
):
    app, transport = group_bridge(tmp_path)
    app.personal.vision.configure("10.00")
    monkeypatch.setattr(personal_vision, "api_key", lambda root: "fixture-key")
    monkeypatch.setattr(personal_vision, "describe", describe_fixture)
    monkeypatch.setattr(
        personal_provider, "answer", lambda *args: pytest.fail("no ChatGPT image call")
    )
    value = group_message(1, "分析一下这张图片")
    value["message"].append({"type": "image", "data": {"file": "fixture"}})
    assert app.accept(value) and app.process_one() and app.personal.tick()
    app.collect_results()
    app.online = True
    while app.deliver_one():
        pass
    assert all(action == "send_group_msg" for action, body in transport.calls)
    assert app.charts.status()["calls_used"] == 0
    assert app.personal.vision.status()["calls_used"] == 1


def test_bare_group_image_waits_and_other_members_cannot_reuse_it(
    tmp_path, monkeypatch
):
    app, _ = group_bridge(tmp_path)
    app.personal.vision.configure("10.00")
    monkeypatch.setattr(personal_vision, "api_key", lambda root: "fixture-key")
    monkeypatch.setattr(personal_vision, "describe", describe_fixture)
    value = group_message(1, "")
    value["message"].append({"type": "image", "data": {"file": "fixture"}})
    assert app.accept(value) and app.process_one() and not app.personal.tick()
    assert (
        app.accept(group_message(2, "/识图 question", user_id=int(OTHER)))
        and app.process_one()
    )
    assert not app.personal.tick()
    assert app.accept(group_message(3, "/识图 question")) and app.process_one()
    assert app.personal.tick() and app.personal.vision.status()["calls_used"] == 1


def test_private_bare_image_preserves_unpaid_stock_and_vision_choice(
    tmp_path, monkeypatch
):
    app, _ = group_bridge(tmp_path)
    app.personal.vision.configure("10.00")
    monkeypatch.setattr(personal_vision, "api_key", lambda root: "fixture-key")
    monkeypatch.setattr(personal_vision, "describe", describe_fixture)
    assert (
        app.accept(event(1, image=True))
        and app.process_one()
        and not app.personal.tick()
    )
    assert app.charts.status()["calls_used"] == 0

    assert (
        app.accept(event(2, "/识图 question"))
        and app.process_one()
        and app.personal.tick()
    )
    assert app.charts.status()["calls_used"] == 0


@pytest.mark.parametrize("case", ["bad_json", "empty", "length", "tools"])
def test_incomplete_vision_response_is_not_repaired_or_retried(monkeypatch, case):
    value = {
        "choices": [{"finish_reason": "stop", "message": {"content": "answer"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    }
    if case == "empty":
        value["choices"] = []
    elif case == "length":
        value["choices"][0]["finish_reason"] = "length"
    elif case == "tools":
        value["choices"][0]["message"]["tool_calls"] = [{}]
    raw = b"not-json" if case == "bad_json" else json.dumps(value).encode()
    calls = []

    class Opener:
        def open(self, request, timeout):
            calls.append(1)
            return io.BytesIO(raw)

    monkeypatch.setattr(
        personal_vision.urllib.request, "build_opener", lambda *args: Opener()
    )
    with pytest.raises(PlatformError, match="PERSONAL_VISION_RESPONSE_INVALID"):
        personal_vision.describe("fixture-key", image_bytes(), "image/png", "question")
    assert calls == [1]
