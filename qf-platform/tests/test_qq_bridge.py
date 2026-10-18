import base64
import copy
import hmac
import json
import threading
import urllib.error
import urllib.request
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from test_chart_analysis import fixture_report, image_payload

from qf_platform import chart_provider
from qf_platform.qq_bridge import QQBridge
from qf_platform.qq_protocol import (
    QQConfig,
    chart_metadata,
    private_event,
    verify_signature,
)
from qf_platform.qq_server import make_server
from qf_platform.qq_status import connected
from qf_platform.qq_transport import OneBot, download_image
from qf_platform.storage import PlatformError

BOT = "123456789"
USER = "3140000001"
TOKEN = "fixture-only-token-with-at-least-32-characters"


class Transport(OneBot):
    def __init__(self):
        self.calls = []
        self.fail = False

    def image(self, segment):
        return base64.b64decode(image_payload()["image_base64"])

    def call(self, action, parameters):
        self.calls.append((action, parameters))
        if self.fail:
            raise PlatformError("QQ_ACTION_EFFECT_UNKNOWN")
        data = (
            {"user_id": BOT}
            if action == "get_login_info"
            else {"online": True}
            if action == "get_status"
            else {"message_id": 1}
        )
        return {"status": "ok", "retcode": 0, "data": data}


def event(number=1, text="", image=False, **changes):
    message = [{"type": "text", "data": {"text": text}}]
    if image:
        message.append(
            {
                "type": "image",
                "data": {"file": "fixture.png", "url": "https://gchat.qpic.cn/fixture"},
            }
        )
    return {
        "post_type": "message",
        "message_type": "private",
        "self_id": int(BOT),
        "user_id": int(USER),
        "message_id": number,
        "time": int(datetime.now(UTC).timestamp()),
        "message": message,
        **changes,
    }


def bridge(tmp_path):
    transport = Transport()
    config = QQConfig(BOT, (USER,), TOKEN, enabled=True)
    return QQBridge(tmp_path, config, transport), transport


def test_hmac_and_private_allowlist_reject_unauthorized_events():
    config = QQConfig(BOT, (USER,), TOKEN, enabled=True)
    value = event()
    raw = json.dumps(value).encode()
    signature = "sha1=" + hmac.new(TOKEN.encode(), raw, "sha1").hexdigest()
    verify_signature(raw, signature, TOKEN)
    with pytest.raises(PlatformError):
        verify_signature(raw + b" ", signature, TOKEN)
    assert private_event(value, config)
    for changes in (
        {"user_id": 999999},
        {"self_id": 999999},
        {"message_type": "group"},
        {"user_id": BOT},
        {"time": 0},
        {"post_type": "message_sent"},
    ):
        assert not private_event(event(**changes), config)
    assert not private_event(value, QQConfig(BOT, (USER,), TOKEN))


def test_normalization_splits_forecast_holding_and_adjustment():
    value = chart_metadata(
        "/分析 600519 2026-09-01 2026-09-30 不复权 持仓=10 成本=100 已持=7"
    )
    assert value["horizon_bars"] == 5 and value["max_holding_bars"] == 10
    assert value["adjustment"] == "RAW" and value["held_bars"] == 7
    for invalid in (
        "/分析 600519 2026-09-01 2026-09-30",
        "/分析 600519 2026-09-30 2026-09-01 不复权",
        "/分析 600519 2026-09-01 2026-09-30 不复权 持仓=999",
        "/分析 600519 2026-09-01 2026-09-30 不复权 已持=7",
    ):
        with pytest.raises(PlatformError):
            chart_metadata(invalid)


def test_image_then_parameters_deduplicates_without_model_execution(tmp_path):
    app, transport = bridge(tmp_path)
    value = event(image=True)
    assert app.accept(value) and not app.accept(value)
    assert app.process_one() and app.charts.status()["calls_used"] == 0
    assert app.charts.list()["items"] == []
    parameters = event(2, "/分析 600519 2026-09-01 2026-09-30 不复权 持仓=10")
    assert app.accept(parameters)
    assert app.process_one()
    records = app.charts.list()["items"]
    assert len(records) == 1 and records[0]["status"] == "WAITING_FOR_MODEL"
    assert records[0]["request"]["channel"] == "QQ"
    restored = QQBridge(tmp_path, app.config, transport)
    assert not restored.accept(parameters) and not restored.process_one()
    assert app.charts.status()["calls_used"] == 0
    altered = copy.deepcopy(parameters)
    altered["message"][0]["data"]["text"] += " 修改"
    with pytest.raises(PlatformError, match="IDEMPOTENCY"):
        restored.accept(altered)


def test_result_delivery_and_retrieval_use_same_report_and_never_another_model_call(
    tmp_path, monkeypatch
):
    from qf_platform import chart_algorithms

    app, transport = bridge(tmp_path)
    app.charts.configure({"api_key": "fixture-only", "max_cost_cny": "10.00"})
    monkeypatch.setattr(
        chart_algorithms,
        "analyze",
        lambda *args, **kwargs: {
            "status": "UNAVAILABLE",
            "reason": "software fixture",
            "strategies": [],
            "source": None,
        },
    )
    monkeypatch.setattr(
        chart_provider,
        "analyze",
        lambda *args: (fixture_report(), {"prompt_tokens": 1, "completion_tokens": 1}),
    )
    assert app.accept(event(1, "/分析 600519 2026-09-01 2026-09-30 不复权", image=True))
    assert app.process_one() and app.charts.tick()
    job = app.charts.get(app.charts.list()["items"][0]["id"])
    app.collect_results()
    app.collect_results()
    app.probe()
    while app.deliver_one():
        pass
    uploads = [x for x in transport.calls if x[0] == "upload_private_file"]
    assert len(uploads) == 1
    document = base64.b64decode(
        uploads[0][1]["file"].removeprefix("base64://")
    ).decode()
    assert job["image"]["sha256"] in document and job["report"]["summary"] in document
    assert all(x[1].get("user_id", USER) == USER for x in transport.calls)
    before = app.charts.status()
    assert app.accept(event(2, "/报告 " + job["id"])) and app.process_one()
    while app.deliver_one():
        pass
    assert app.charts.status() == before and app.charts.status()["calls_used"] == 1
    assert len([x for x in transport.calls if x[0] == "upload_private_file"]) == 2


def test_unknown_delivery_is_not_replayed_after_restart(tmp_path):
    app, transport = bridge(tmp_path)
    assert app.accept(event(text="/帮助")) and app.process_one()
    app.online = True
    transport.fail = True
    assert app.deliver_one() and not app.deliver_one()
    restored = QQBridge(tmp_path, app.config, transport)
    restored.online = True
    assert not restored.deliver_one()
    with closing(restored.connect()) as db:
        assert db.execute("SELECT state FROM outbox").fetchone()[0] == "UNKNOWN"
    assert len(transport.calls) == 1


def test_restart_does_not_replay_inflight_send_and_does_not_create_interrupted_task(
    tmp_path,
):
    app, transport = bridge(tmp_path)
    assert app.accept(event(text="/帮助")) and app.process_one()
    with closing(app.connect()) as db, db:
        db.execute("UPDATE outbox SET state='SENDING'")
    assert app.accept(event(2, "/分析 600519 2026-09-01 2026-09-30 不复权"))
    with closing(app.connect()) as db, db:
        db.execute("UPDATE inbox SET state='PROCESSING' WHERE state='NEW'")
    restored = QQBridge(tmp_path, app.config, transport)
    assert not restored.process_one() and not restored.deliver_one()
    assert restored.charts.list()["items"] == []
    with closing(restored.connect()) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM inbox WHERE state='REVIEW_REQUIRED'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1/key",
        "https://evil.invalid/image",
        "https://qpic.cn.evil.invalid/image",
        "https://gchat.qpic.cn:9999/image",
        "https://multimedia.nt.qq.com.cn.evil.invalid/download",
        "https://other.qq.com.cn/download",
    ],
)
def test_remote_image_access_rejects_local_or_untrusted_hosts(url):
    with pytest.raises(PlatformError, match="QQ_IMAGE_URL_INVALID"):
        download_image(url)


def test_local_dns_and_redirect_are_not_followed(monkeypatch):
    import socket

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(PlatformError, match="QQ_IMAGE_URL_INVALID"):
        download_image("https://gchat.qpic.cn/image")


@pytest.mark.parametrize(
    "host,address,allow_fake",
    [
        ("multimedia.nt.qq.com.cn", "119.147.0.1", False),
        ("multimedia.nt.qq.com", "119.147.0.1", False),
        ("multimedia.nt.qq.com.cn", "198.18.0.221", True),
        ("multimedia.nt.qq.com.cn", "198.19.255.255", True),
    ],
)
def test_qq_multimedia_exact_domains_download_bounded_https(
    monkeypatch, host, address, allow_fake
):
    from qf_platform import qq_transport
    from qf_platform.chart_reports import MAX_IMAGE_BYTES

    calls = []
    data = base64.b64decode(image_payload()["image_base64"])

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, limit):
            assert limit == MAX_IMAGE_BYTES + 1
            return data

    class Opener:
        def open(self, request, timeout):
            calls.append(request.full_url)
            assert timeout == 20 and request.get_header("Authorization") is None
            return Response()

    monkeypatch.setattr(
        qq_transport.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(2, 1, 6, "", (address, 443))],
    )
    monkeypatch.setattr(
        qq_transport.urllib.request, "build_opener", lambda *args: Opener()
    )
    url = "https://" + host + "/download?fileid=fixture-only"
    assert download_image(url, allow_fake_ip_cdn=allow_fake) == data and calls == [url]


@pytest.mark.parametrize(
    "address,enabled",
    [
        ("198.18.0.221", False),
        ("127.0.0.1", True),
        ("10.0.0.1", True),
        ("169.254.169.254", True),
    ],
)
def test_fake_ip_compatibility_is_opt_in_and_keeps_other_private_ranges_blocked(
    monkeypatch, address, enabled
):
    import socket

    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *args, **kwargs: [(2, 1, 6, "", (address, 443))]
    )
    with pytest.raises(PlatformError, match="QQ_IMAGE_URL_INVALID"):
        download_image(
            "https://multimedia.nt.qq.com.cn/download", allow_fake_ip_cdn=enabled
        )


def test_fake_ip_config_defaults_off_and_requires_a_boolean(tmp_path):
    path = tmp_path / "qq.json"
    value = {"bot_id": BOT, "allowed_users": [USER], "token": TOKEN, "enabled": True}
    path.write_text(json.dumps(value), encoding="utf-8")
    assert QQConfig.load(path).allow_fake_ip_cdn is False
    value["allow_fake_ip_cdn"] = True
    path.write_text(json.dumps(value), encoding="utf-8")
    assert QQConfig.load(path).allow_fake_ip_cdn is True
    value["allow_fake_ip_cdn"] = "yes"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(PlatformError, match="QQ_CONFIG_INVALID"):
        QQConfig.load(path)


def test_image_rejection_has_actionable_reply_without_creating_a_model_job(tmp_path):
    app, transport = bridge(tmp_path)

    def rejected(segment):
        raise PlatformError("QQ_IMAGE_URL_INVALID")

    transport.image = rejected
    assert app.accept(event(1, "/分析 600519 2026-09-01 2026-09-30 不复权", image=True))
    assert app.process_one()
    assert app.charts.list()["items"] == [] and app.charts.status()["calls_used"] == 0
    with closing(app.connect()) as db:
        raw = db.execute("SELECT body FROM outbox").fetchone()[0]
    text = json.loads(raw)["message"][0]["data"]["text"]
    assert "尚未调用识图模型" in text and "请联系维护者" in text
    assert "QQ_IMAGE_URL_INVALID" not in text and "/分析" not in text


def test_http_receiver_checks_exact_signature_before_persisting(tmp_path):
    app, _ = bridge(tmp_path)
    server = make_server(app, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    raw = json.dumps(event(text="/帮助")).encode()
    url = f"http://127.0.0.1:{server.server_port}/events"
    try:
        headers = {"X-Self-ID": BOT, "Content-Type": "application/json"}
        with pytest.raises(urllib.error.HTTPError) as error:
            op.open(urllib.request.Request(url, data=raw, headers=headers))
        assert error.value.code == 403
        headers["X-Signature"] = (
            "sha1=" + hmac.new(TOKEN.encode(), raw, "sha1").hexdigest()
        )
        for _ in range(2):
            with op.open(
                urllib.request.Request(url, data=raw, headers=headers)
            ) as response:
                assert response.status == 200
        assert app.process_one() and not app.process_one()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_liveness_expires_without_exposing_tokens(tmp_path):
    app, _ = bridge(tmp_path)
    app.probe()
    assert connected(tmp_path) and app.charts.status()["qq_connected"]
    path = app.root / "connection.json"
    value = json.loads(path.read_bytes())
    assert TOKEN not in path.read_text() and BOT not in path.read_text()
    value["observed_at"] = (datetime.now(UTC) - timedelta(seconds=46)).isoformat()
    path.write_text(json.dumps(value), encoding="utf-8")
    assert not connected(tmp_path)


def test_napcat_node_style_chunked_events_keep_signature_and_bounds(tmp_path):
    import http.client
    import socket

    app, _ = bridge(tmp_path)
    server = make_server(app, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    raw = json.dumps(event(text="/帮助")).encode()
    try:
        connection = http.client.HTTPConnection(
            "127.0.0.1", server.server_port, timeout=3
        )
        headers = {
            "X-Self-ID": BOT,
            "X-Signature": "sha1=" + hmac.new(TOKEN.encode(), raw, "sha1").hexdigest(),
        }
        connection.request(
            "POST",
            "/events",
            body=iter([raw[:20], raw[20:]]),
            headers=headers,
            encode_chunked=True,
        )
        response = connection.getresponse()
        assert response.status == 200
        response.read()
        connection.close()
        assert app.process_one() and not app.process_one()
        for framing in [
            b"Content-Length: 10\r\nTransfer-Encoding: chunked\r\n",
            b"Transfer-Encoding: chunked\r\n",
        ]:
            with socket.create_connection(
                ("127.0.0.1", server.server_port), timeout=3
            ) as peer:
                peer.sendall(
                    b"POST /events HTTP/1.1\r\nHost: localhost\r\n"
                    + framing
                    + b"\r\n30001\r\n"
                )
                result = peer.recv(2048)
                assert (
                    b"400" in result
                    if b"Content-Length" in framing
                    else b"413" in result
                )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
