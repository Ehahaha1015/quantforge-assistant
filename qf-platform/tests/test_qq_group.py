import copy
import json
from contextlib import closing
from dataclasses import replace

import pytest
from test_personal_assistant import account
from test_qq_bridge import BOT, TOKEN, USER, bridge, event

from qf_platform import personal_provider
from qf_platform.personal_service import PersonalService
from qf_platform.qq_bridge import QQBridge
from qf_platform.qq_protocol import QQConfig, group_event, private_event
from qf_platform.qq_transport import OneBot
from qf_platform.storage import PlatformError

GROUP = "987654321"
OTHER = "3140000002"


def group_message(number=1, text="hello", **changes):
    value = event(
        number, text, message_type="group", sub_type="normal", group_id=int(GROUP)
    )
    value["message"].insert(0, {"type": "at", "data": {"qq": BOT}})
    return {**value, **changes}


def group_bridge(tmp_path):
    old, transport = bridge(tmp_path)
    config = replace(
        old.config,
        personal_enabled=True,
        group_enabled=True,
        allowed_groups=(GROUP,),
        group_allowed_users=(USER, OTHER),
    )
    app = QQBridge(tmp_path, config, transport)
    app.personal = PersonalService(tmp_path, account(tmp_path))
    return app, transport


def test_group_requires_exact_group_member_normal_event_and_actual_mention(tmp_path):
    app, _ = group_bridge(tmp_path)
    message = group_message()
    assert group_event(message, app.config) and not private_event(message, app.config)
    variants = [
        {"group_id": 1234567},
        {"user_id": 7777777},
        {"user_id": BOT},
        {"self_id": 7777777},
        {"post_type": "message_sent"},
        {"sub_type": "anonymous"},
        {"sub_type": "notice"},
        {"anonymous": {}},
        {"time": 0},
        {"message": [{"type": "text", "data": {"text": "@" + BOT}}]},
        {"message": [{"type": "at", "data": {"qq": "all"}}]},
        {"message": [{"type": "at", "data": {"qq": OTHER}}]},
    ]
    for change in variants:
        assert not group_event({**message, **change}, app.config)
        assert not app.accept({**message, **change})
    assert not group_event(message, replace(app.config, group_enabled=False))
    assert not group_event(message, replace(app.config, personal_enabled=False))
    assert not private_event(event(user_id=OTHER), app.config)


def test_group_configuration_is_off_by_default_and_bounded(tmp_path):
    path = tmp_path / "bridge.json"
    value = {"bot_id": BOT, "allowed_users": [USER], "token": TOKEN, "enabled": True}
    path.write_text(json.dumps(value))
    assert not QQConfig.load(path).group_enabled
    value.update(
        personal_enabled=True,
        group_enabled=True,
        allowed_groups=[GROUP],
        group_allowed_users=[USER, OTHER],
    )
    path.write_text(json.dumps(value))
    assert QQConfig.load(path).group_allowed_users == (USER, OTHER)
    for changes in (
        {"allowed_groups": []},
        {"allowed_groups": [GROUP, "1234567"]},
        {"group_allowed_users": []},
        {"group_allowed_users": [BOT]},
        {"personal_enabled": False},
        {"group_enabled": "yes"},
    ):
        path.write_text(json.dumps({**value, **changes}))
        with pytest.raises(PlatformError):
            QQConfig.load(path)


def test_group_and_private_contexts_and_members_never_mix(tmp_path, monkeypatch):
    app, transport = group_bridge(tmp_path)
    seen = []

    def answer(account, messages, model, token):
        seen.append(copy.deepcopy(messages))
        return "answer:" + messages[-1]["content"], {}

    monkeypatch.setattr(personal_provider, "answer", answer)
    for message in (
        event(1, "PRIVATE_SECRET"),
        group_message(1, "GROUP_A"),
        group_message(2, "GROUP_B", user_id=int(OTHER)),
        group_message(3, "GROUP_A_NEXT"),
    ):
        assert app.accept(message) and app.process_one() and app.personal.tick()
    assert [x[0]["content"] for x in seen[:3]] == [
        "PRIVATE_SECRET",
        "GROUP_A",
        "GROUP_B",
    ]
    assert len(seen[0]) == len(seen[1]) == len(seen[2]) == 1
    assert [x["content"] for x in seen[3]] == [
        "GROUP_A",
        "answer:GROUP_A",
        "GROUP_A_NEXT",
    ]
    app.collect_results()
    app.collect_results()
    app.online = True
    while app.deliver_one():
        pass
    groups = [body for action, body in transport.calls if action == "send_group_msg"]
    assert groups and all(body["group_id"] == GROUP for body in groups)
    assert all(body["message"][0]["type"] == "at" for body in groups)
    assert all("PRIVATE_SECRET" not in json.dumps(body) for body in groups)
    assert app.charts.status()["calls_used"] == 0


def test_group_recent_answer_and_new_dialogue_do_not_expose_private_history(
    tmp_path, monkeypatch
):
    app, _ = group_bridge(tmp_path)
    monkeypatch.setattr(
        personal_provider, "answer", lambda *args: ("PRIVATE_SECRET", {})
    )
    app.accept(event(1, "private question"))
    app.process_one()
    app.personal.tick()
    with app.personal.connect() as db:
        private_session = db.execute(
            "SELECT current FROM sessions WHERE owner=?", (USER,)
        ).fetchone()
    for message in (group_message(2, "/最近回答"), group_message(3, "/新对话")):
        assert app.accept(message) and app.process_one()
    with app.connect() as db:
        bodies = [
            json.loads(row[0])
            for row in db.execute(
                "SELECT body FROM outbox WHERE action='send_group_msg'"
            )
        ]
    assert all("PRIVATE_SECRET" not in json.dumps(body) for body in bodies)
    with app.personal.connect() as db:
        assert (
            db.execute("SELECT current FROM sessions WHERE owner=?", (USER,)).fetchone()
            == private_session
        )
    assert not app.personal.tick()


def test_group_images_chart_commands_and_model_changes_do_not_call_models(
    tmp_path, monkeypatch
):
    app, transport = group_bridge(tmp_path)
    monkeypatch.setattr(
        transport, "image", lambda *args: pytest.fail("group must not download images")
    )
    before = app.personal.account.read("settings.json")
    for number, text in enumerate(
        ("/分析 600519 2026-09-01 2026-09-30 不复权", "/模型 fixture-model", "/帮助"), 1
    ):
        assert app.accept(group_message(number, text)) and app.process_one()
    image = group_message(4, "/分析 600519 2026-09-01 2026-09-30 不复权")
    image["message"].append({"type": "image", "data": {"file": "fixture"}})
    assert app.accept(image) and app.process_one()
    assert not app.personal.tick() and app.charts.status()["calls_used"] == 0
    assert app.personal.account.read("settings.json") == before
    with app.connect() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM outbox WHERE action!=?", ("send_group_msg",)
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("tamper", ["revoke", "other_group", "private_action"])
def test_group_delivery_rechecks_authorization_and_destination(tmp_path, tamper):
    app, transport = group_bridge(tmp_path)
    assert app.accept(group_message(1, "/帮助")) and app.process_one()
    app.online = True
    if tamper == "revoke":
        app.config = replace(app.config, group_allowed_users=(OTHER,))
    else:
        with closing(app.connect()) as db, db:
            key, raw = db.execute("SELECT id,body FROM outbox").fetchone()
            body = json.loads(raw)
            if tamper == "other_group":
                body["group_id"] = "1234567"
                action = "send_group_msg"
            else:
                body = {"user_id": USER, "message": body["message"]}
                action = "send_private_msg"
            db.execute(
                "UPDATE outbox SET action=?,body=? WHERE id=?",
                (action, json.dumps(body), key),
            )
    assert app.deliver_one() and not transport.calls
    with app.connect() as db:
        assert db.execute("SELECT state FROM outbox").fetchone()[0] == "CANCELLED"


def test_group_duplicate_unknown_send_is_not_replayed(tmp_path, monkeypatch):
    app, transport = group_bridge(tmp_path)
    value = group_message(1, "/帮助")
    assert app.accept(value) and not app.accept(value) and app.process_one()
    with closing(app.connect()) as db, db:
        db.execute("UPDATE outbox SET state='SENDING'")
    restarted = QQBridge(tmp_path, app.config, transport)
    restarted.online = True
    assert not restarted.deliver_one() and not transport.calls
    with restarted.connect() as db:
        assert db.execute("SELECT state FROM outbox").fetchone()[0] == "UNKNOWN"


def test_account_cap_is_shared_by_private_group_and_members(tmp_path, monkeypatch):
    app, _ = group_bridge(tmp_path)
    calls = []
    monkeypatch.setattr(
        personal_provider, "answer", lambda *args: (calls.append(1) or "fixture", {})
    )
    for number in range(21):
        message = (
            event(number, "question")
            if number % 3 == 0
            else group_message(
                number, "question", user_id=int(USER if number % 3 == 1 else OTHER)
            )
        )
        assert app.accept(message) and app.process_one() and app.personal.tick()
    assert len(calls) == 20
    assert app.personal.status(USER)["account_requests_today"] == 20
    assert app.personal.results()[-1]["error"] == "PERSONAL_ACCOUNT_DAILY_LIMIT"
    assert app.charts.status()["calls_used"] == 0


def test_onebot_group_actions_are_limited_to_configured_group(tmp_path):
    app, _ = group_bridge(tmp_path)
    for action in ("send_group_msg", "get_group_info"):
        with pytest.raises(PlatformError, match="QQ_GROUP_DISABLED"):
            OneBot(app.config).call(action, {"group_id": "1234567"})
        with pytest.raises(PlatformError, match="QQ_GROUP_DISABLED"):
            OneBot(replace(app.config, group_enabled=False)).call(
                action, {"group_id": GROUP}
            )
    with pytest.raises(PlatformError, match="QQ_ACTION_FORBIDDEN"):
        OneBot(app.config).call("set_group_add_request", {})
