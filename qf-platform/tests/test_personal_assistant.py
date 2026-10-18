import base64
import io
import json
import sqlite3
import time
import urllib.error
from contextlib import closing
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from test_qq_bridge import bridge, event

from qf_platform import personal_auth, personal_provider
from qf_platform.personal_auth import (
    ISSUER,
    PLAN_SCOPE,
    AccountStore,
    Authorization,
    verify_identity,
)
from qf_platform.personal_service import PersonalService
from qf_platform.qq_bridge import QQBridge
from qf_platform.storage import PlatformError


def encode(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def signed_identity(key, **changes):
    claims = {
        "iss": ISSUER,
        "aud": "oaiapp_fixture",
        "exp": time.time() + 600,
        "sub": "fixture-person",
        "nonce": "fixture-nonce",
        **changes,
    }
    header = encode(json.dumps({"alg": "RS256", "kid": "fixture-key"}).encode())
    body = encode(json.dumps(claims).encode())
    return (
        header
        + "."
        + body
        + "."
        + encode(
            key.sign(
                (header + "." + body).encode(), padding.PKCS1v15(), hashes.SHA256()
            )
        )
    )


@pytest.fixture
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwks(key):
    public = key.public_key().public_numbers()
    return {
        "keys": [
            {
                "kid": "fixture-key",
                "kty": "RSA",
                "n": encode(public.n.to_bytes((public.n.bit_length() + 7) // 8)),
                "e": encode(public.e.to_bytes(3)),
            }
        ]
    }


def account(tmp_path):
    store = AccountStore(tmp_path / "account")
    store.write(
        "credentials.enc",
        {
            "subject": "fixture-person",
            "client_id": "oaiapp_fixture",
            "scopes": [PLAN_SCOPE],
            "access_token": "fixture-private-token",
            "refresh_token": "fixture-refresh",
            "refresh_state": "READY",
            "expires_at": time.time() + 600,
        },
    )
    store.write(
        "models.json",
        {"models": [{"slug": "fixture-model", "display_name": "Fixture"}]},
    )
    store.write("settings.json", {"model": "fixture-model"})
    return store


def test_id_token_requires_verified_signature_and_matching_claims(signing_key):
    token = signed_identity(signing_key)
    assert (
        verify_identity(
            token, "oaiapp_fixture", nonce="fixture-nonce", jwks=jwks(signing_key)
        )["sub"]
        == "fixture-person"
    )
    for changes in (
        {"iss": "https://wrong.invalid"},
        {"aud": "other-client"},
        {"exp": 0},
        {"nonce": "other"},
        {"sub": "other-person"},
    ):
        with pytest.raises(PlatformError, match="ID_TOKEN_INVALID"):
            verify_identity(
                signed_identity(signing_key, **changes),
                "oaiapp_fixture",
                nonce="fixture-nonce",
                expected_subject="fixture-person",
                jwks=jwks(signing_key),
            )
    wrong = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(PlatformError):
        verify_identity(
            token, "oaiapp_fixture", nonce="fixture-nonce", jwks=jwks(wrong)
        )


def test_oauth_state_pkce_and_issued_client_are_separate(
    tmp_path, monkeypatch, signing_key
):
    from urllib.parse import parse_qs, urlsplit

    store = AccountStore(tmp_path / "account")
    flow = Authorization(store, "http://127.0.0.1:1455/auth/callback")
    fields = parse_qs(urlsplit(flow.url()).query)
    assert fields["client_id"] == ["dynamic_agent_client"] and fields["resource"] == [
        "https://api.openai.com/v1"
    ]
    assert fields["code_challenge"][0] != flow.verifier and fields[
        "code_challenge_method"
    ] == ["S256"]
    with pytest.raises(PlatformError, match="STATE_INVALID"):
        flow.complete({"state": "wrong", "code": "fixture"})
    calls = []

    def fetch(url, *, form=None, token=None):
        calls.append((url, form))
        if url == personal_auth.TOKEN_ENDPOINT:
            assert (
                form["client_id"] == "oaiapp_fixture"
                and form["redirect_uri"] == flow.redirect_uri
            )
            return {
                "access_token": "fixture-secret",
                "id_token": signed_identity(signing_key, nonce=flow.nonce),
                "expires_in": 600,
                "scope": PLAN_SCOPE,
            }
        if url == personal_auth.JWKS_ENDPOINT:
            return jwks(signing_key)
        return {
            "models": [
                {
                    "slug": "fixture-model",
                    "display_name": "Fixture",
                    "visibility": "list",
                }
            ]
        }

    monkeypatch.setattr(personal_auth, "fetch_json", fetch)
    flow.complete(
        {"state": flow.state, "code": "fixture", "client_id": "oaiapp_fixture"}
    )
    assert (
        store.status()["connected"]
        and store.read("registration.json")["client_id"] == "oaiapp_fixture"
    )
    assert b"fixture-secret" not in (store.root / "credentials.enc").read_bytes()
    with pytest.raises(PlatformError):
        flow.complete(
            {"state": flow.state, "code": "fixture", "client_id": "oaiapp_fixture"}
        )
    assert Authorization(store, flow.redirect_uri).host_id == flow.host_id


def stream(*events):
    return io.BytesIO(
        b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events)
    )


def test_stream_only_returns_a_completed_answer():
    delta = {"type": "response.output_text.delta", "delta": "software fixture"}
    completed = {
        "type": "response.completed",
        "response": {"status": "completed", "output": [], "usage": {"total_tokens": 9}},
    }
    assert personal_provider.parse_stream(stream(delta, completed)) == (
        "software fixture",
        {"total_tokens": 9},
    )
    for terminal in (
        None,
        {"type": "response.failed"},
        {"type": "response.incomplete"},
    ):
        with pytest.raises(PlatformError):
            personal_provider.parse_stream(
                stream(delta, *([terminal] if terminal else []))
            )


def test_request_uses_plan_oauth_without_unsupported_fields_or_tools(
    tmp_path, monkeypatch
):
    store = account(tmp_path)
    captured = []

    class Response(io.BytesIO):
        pass

    class Opener:
        def open(self, request, timeout):
            captured.append(json.loads(request.data))
            assert request.get_header("Authorization") == "Bearer fixture-private-token"
            return Response(
                stream(
                    {"type": "response.output_text.delta", "delta": "answer"},
                    {
                        "type": "response.completed",
                        "response": {"status": "completed", "output": []},
                    },
                ).read()
            )

    monkeypatch.setattr(
        personal_provider.urllib.request, "build_opener", lambda *args: Opener()
    )
    text, _ = personal_provider.answer(
        store,
        [{"role": "user", "content": "hello"}],
        "fixture-model",
        "fixture-private-token",
    )
    assert text == "answer" and len(captured) == 1
    value = captured[0]
    assert value["store"] is False and value["stream"] is True and value["tools"] == []
    assert not {
        "max_output_tokens",
        "previous_response_id",
        "conversation",
        "temperature",
    } & set(value)


def test_context_is_per_owner_and_new_session_keeps_journal(tmp_path, monkeypatch):
    store = account(tmp_path)
    service = PersonalService(tmp_path, store)
    seen = []

    def answer(account, messages, model, token):
        seen.append(messages)
        return "fixture-answer", {"total_tokens": 3}

    monkeypatch.setattr(personal_provider, "answer", answer)
    assert service.queue("alice", "one", "question1") == "one"
    assert service.tick()
    service.queue("alice", "two", "question2")
    assert service.tick()
    assert [x["content"] for x in seen[1]] == [
        "question1",
        "fixture-answer",
        "question2",
    ]
    service.queue("bob", "three", "other")
    service.tick()
    assert len(seen[2]) == 1
    service.new_session("alice")
    service.queue("alice", "four", "fresh")
    service.tick()
    assert len(seen[3]) == 1
    with closing(service.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM request_journal").fetchone()[0] == 8
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM request_journal")


def test_unknown_request_is_not_replayed_and_unauthorized_account_cannot_queue(
    tmp_path, monkeypatch
):
    service = PersonalService(tmp_path, account(tmp_path))
    calls = []

    def interrupted(*args):
        calls.append(1)
        raise PlatformError("PERSONAL_RESPONSE_UNCONFIRMED")

    monkeypatch.setattr(personal_provider, "answer", interrupted)
    service.queue("alice", "one", "hello")
    assert service.tick() and not service.tick()
    assert service.results()[0]["status"] == "UNKNOWN" and calls == [1]
    service.queue("alice", "two", "next")
    with closing(service.connect()) as db, db:
        db.execute(
            "UPDATE turns SET status='RUNNING',sent_day='2026-10-17' WHERE id='two'"
        )
    assert service.tick() and not service.tick() and calls == [1]
    (tmp_path / "other").mkdir()
    empty = PersonalService(tmp_path / "other", AccountStore(tmp_path / "empty"))
    with pytest.raises(PlatformError, match="AUTH_REQUIRED"):
        empty.queue("alice", "one", "hello")


def test_private_qq_routing_does_not_use_chart_model_or_mix_history(
    tmp_path, monkeypatch
):
    app, transport = bridge(tmp_path)
    config = replace(app.config, personal_enabled=True)
    app = QQBridge(tmp_path, config, transport)
    app.personal = PersonalService(tmp_path, account(tmp_path))
    before = app.charts.status()
    assert app.accept(event(1, "hello")) and app.process_one()
    monkeypatch.setattr(personal_provider, "answer", lambda *args: ("a" * 3500, {}))
    assert app.personal.tick()
    app.collect_results()
    app.collect_results()
    with closing(app.connect()) as db:
        rows = db.execute(
            "SELECT body FROM outbox WHERE id LIKE 'personal-%'"
        ).fetchall()
    assert len(rows) == 3 and all(
        len(json.loads(row[0])["message"][0]["data"]["text"]) < 2000 for row in rows
    )
    assert app.charts.status() == before
    assert app.accept(event(2, "/帮助")) and app.process_one()
    assert not app.personal.tick()


def test_daily_limit_retains_all_sent_requests(tmp_path, monkeypatch):
    service = PersonalService(tmp_path, account(tmp_path))
    calls = []

    def answer(*args):
        calls.append(1)
        return "fixture", {}

    monkeypatch.setattr(personal_provider, "answer", answer)
    for index in range(21):
        service.queue("alice", "turn-" + str(index), "question")
        assert service.tick()
    assert len(calls) == 20 and service.status("alice")["requests_today"] == 20
    assert service.results()[-1]["status"] == "FAILED"
    with closing(service.connect()) as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM request_journal WHERE kind='REQUEST_STARTED'"
            ).fetchone()[0]
            == 20
        )


def test_ambiguous_refresh_requires_reauthentication_without_replay(
    tmp_path, monkeypatch
):
    store = account(tmp_path)
    value = store.read("credentials.enc")
    value["expires_at"] = 0
    store.write("credentials.enc", value)
    calls = []

    def lost(*args, **kwargs):
        calls.append(1)
        raise PlatformError("PERSONAL_AUTH_RESPONSE_UNKNOWN")

    monkeypatch.setattr(personal_auth, "fetch_json", lost)
    for _ in range(2):
        with pytest.raises(PlatformError):
            store.access_token()
    assert (
        len(calls) == 1 and store.read("credentials.enc")["refresh_state"] == "UNKNOWN"
    )


def test_qq_retrieval_does_not_generate_a_second_answer(tmp_path, monkeypatch):
    old, transport = bridge(tmp_path)
    app = QQBridge(tmp_path, replace(old.config, personal_enabled=True), transport)
    app.personal = PersonalService(tmp_path, account(tmp_path))
    calls = []

    def answer(*args):
        calls.append(1)
        return "existing fixture answer", {}

    monkeypatch.setattr(personal_provider, "answer", answer)
    app.accept(event(1, "hello"))
    app.process_one()
    app.personal.tick()
    app.collect_results()
    app.accept(event(2, "/最近回答"))
    app.process_one()
    assert calls == [1] and not app.personal.tick()


def test_oauth_identity_without_plan_scope_is_not_inference_authorization(
    tmp_path, monkeypatch, signing_key
):
    store = AccountStore(tmp_path / "account")
    flow = Authorization(store, "http://127.0.0.1:1455/auth/callback")

    def fetch(url, *, form=None, token=None):
        if url == personal_auth.JWKS_ENDPOINT:
            return jwks(signing_key)
        assert url == personal_auth.TOKEN_ENDPOINT
        return {
            "access_token": "fixture",
            "id_token": signed_identity(signing_key, nonce=flow.nonce),
            "expires_in": 600,
            "scope": "openid profile email",
        }

    monkeypatch.setattr(personal_auth, "fetch_json", fetch)
    with pytest.raises(PlatformError, match="PLAN_PERMISSION_NOT_GRANTED"):
        flow.complete(
            {
                "state": flow.state,
                "client_id": "oaiapp_fixture",
                "code": "fixture",
                "scope": PLAN_SCOPE,
            }
        )
    assert store.status()["connected"] is False


def test_auth_http_errors_keep_region_code_without_echoing_secrets(
    tmp_path, monkeypatch
):
    class Opener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(
                request.full_url,
                403,
                "Forbidden",
                {},
                io.BytesIO(
                    json.dumps(
                        {
                            "error": {
                                "code": "unsupported_country_region_territory",
                                "message": "private-token private-code private-email",
                            }
                        }
                    ).encode()
                ),
            )

    monkeypatch.setattr(
        personal_auth.urllib.request, "build_opener", lambda *args: Opener()
    )
    with pytest.raises(PlatformError) as result:
        personal_auth.fetch_json(
            personal_auth.TOKEN_ENDPOINT, form={"code": "private-code"}
        )
    assert (
        str(result.value)
        == "PERSONAL_AUTH_HTTP_403_UNSUPPORTED_COUNTRY_REGION_TERRITORY"
    )
    assert "private" not in str(result.value)
    store = AccountStore(tmp_path / "unconnected")
    store.write("login-status.json", {"state": str(result.value), "connected": False})
    status = PersonalService(tmp_path, store).status("alice")
    assert not status["connected"] and status["login_error"] == str(result.value)
    assert status["requests_today"] == 0
    with pytest.raises(PlatformError, match="AUTH_REQUIRED"):
        PersonalService(tmp_path, store).queue("alice", "blocked", "hello")


@pytest.mark.parametrize(
    "body",
    [b"<html>private-token</html>", b'{"error":{"code":"private-token?secret"}}'],
)
def test_auth_error_ignores_html_and_untrusted_error_prose(body):
    error = urllib.error.HTTPError(
        "https://auth.openai.com", 403, "Forbidden", {}, io.BytesIO(body)
    )
    assert (
        str(personal_auth.http_failure(error, "PERSONAL_AUTH_HTTP_"))
        == "PERSONAL_AUTH_HTTP_403"
    )


def test_personal_proxy_is_explicit_local_and_never_falls_back(monkeypatch):
    monkeypatch.setenv("QF_PERSONAL_PROXY_URL", "http://127.0.0.1:7897")
    handlers = []

    def build(*args):
        handlers.extend(args)
        return object()

    monkeypatch.setattr(personal_auth.urllib.request, "build_opener", build)
    personal_auth.personal_opener()
    assert handlers[0].proxies == {
        "http": "http://127.0.0.1:7897",
        "https": "http://127.0.0.1:7897",
    }
    assert isinstance(handlers[1], personal_auth.NoRedirect)
    for proxy in (
        "http://untrusted.invalid:7897",
        "https://127.0.0.1:7897",
        "http://user:secret@127.0.0.1:7897",
        "http://127.0.0.1:7897/path",
        "http://127.0.0.1:999999",
        "http://127.0.0.1:7897?secret=value",
    ):
        monkeypatch.setenv("QF_PERSONAL_PROXY_URL", proxy)
        with pytest.raises(PlatformError, match="PERSONAL_PROXY_INVALID"):
            personal_auth.personal_opener()
    assert len(handlers) == 2

    class Broken:
        def open(self, request, timeout):
            raise OSError("local proxy unavailable")

    calls = []

    def broken_build(*args):
        calls.append(args)
        return Broken()

    monkeypatch.setenv("QF_PERSONAL_PROXY_URL", "http://host.docker.internal:7897")
    monkeypatch.setattr(personal_auth.urllib.request, "build_opener", broken_build)
    with pytest.raises(PlatformError, match="PERSONAL_AUTH_RESPONSE_UNKNOWN"):
        personal_auth.fetch_json(personal_auth.TOKEN_ENDPOINT, form={"code": "fixture"})
    assert len(calls) == 1


def test_login_callback_handles_chrome_preconnect_without_blocking_oauth(
    tmp_path, monkeypatch, signing_key
):
    import socket
    import threading
    import urllib.request
    from urllib.parse import urlencode

    from qf_platform.personal_login import callback_server

    store = AccountStore(tmp_path / "account")
    server, flow, result = callback_server(store)
    accepted = threading.Event()
    get_request = server.get_request

    def accept():
        connection = get_request()
        accepted.set()
        return connection

    monkeypatch.setattr(server, "get_request", accept)
    exchanges = []

    def fetch(url, *, form=None, token=None):
        if url == personal_auth.JWKS_ENDPOINT:
            return jwks(signing_key)
        if url == personal_auth.TOKEN_ENDPOINT:
            exchanges.append(1)
            return {
                "access_token": "fixture-secret",
                "id_token": signed_identity(signing_key, nonce=flow.nonce),
                "expires_in": 600,
                "scope": PLAN_SCOPE,
            }
        return {"models": [{"slug": "fixture-model", "visibility": "list"}]}

    monkeypatch.setattr(personal_auth, "fetch_json", fetch)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    idle = socket.create_connection(server.server_address, timeout=2)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        assert accepted.wait(2)
        # Keep the first connection open and send no bytes on it.
        with pytest.raises(urllib.error.HTTPError) as bad:
            opener.open(flow.redirect_uri + "?state=wrong&code=fixture", timeout=2)
        assert bad.value.code == 400 and not flow.used and not result
        query = urlencode(
            {"state": flow.state, "code": "fixture", "client_id": "oaiapp_fixture"}
        )
        with opener.open(flow.redirect_uri + "?" + query, timeout=3) as response:
            assert b"account connected" in response.read()
        assert result == {"state": "CONNECTED"} and store.status()["connected"]
        with pytest.raises(urllib.error.HTTPError) as duplicate:
            opener.open(flow.redirect_uri + "?" + query, timeout=2)
        assert duplicate.value.code == 400 and exchanges == [1]
        assert result == {"state": "CONNECTED"}
    finally:
        idle.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_browser_launcher_never_waits_for_chrome_exit(monkeypatch):
    from qf_platform import personal_login

    calls = []

    class RunningChrome:
        def wait(self):
            pytest.fail("OAuth must not wait for the Chrome window to close")

        def poll(self):
            return None

    def start(args, **kwargs):
        calls.append(args)
        return RunningChrome()

    controller = personal_login.webbrowser.GenericBrowser(
        ["fixture-chrome.exe", "--proxy-server=http://127.0.0.1:7897", "%s"]
    )
    monkeypatch.setattr(personal_login.webbrowser, "get", lambda: controller)
    monkeypatch.setattr(personal_login.webbrowser.subprocess, "Popen", start)
    assert personal_login.launch_browser("https://auth.openai.com/fixture")
    assert calls == [
        [
            "fixture-chrome.exe",
            "--proxy-server=http://127.0.0.1:7897",
            "https://auth.openai.com/fixture",
        ]
    ]
