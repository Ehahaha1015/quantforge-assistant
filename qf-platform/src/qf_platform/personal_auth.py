"""Official ChatGPT-plan OAuth credentials, isolated from screenshot/API-key budgets."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, cast

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from quantforge.product.storage import local_root, writer_lock

from .storage import PlatformError, canonical, digest

ISSUER = "https://auth.openai.com"
AUTHORIZE = ISSUER + "/api/accounts/authorize"
TOKEN_ENDPOINT = ISSUER + "/api/accounts/oauth/token"
JWKS_ENDPOINT = ISSUER + "/.well-known/jwks.json"
RESOURCE = "https://api.openai.com/v1"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
APP_NAME = "QuantForge Personal Assistant"


def http_failure(error: urllib.error.HTTPError, prefix: str) -> PlatformError:
    """Keep machine codes, never echo server prose, request bodies or credentials."""
    suffix = ""
    try:
        raw = error.read(65537)
        value = json.loads(raw) if len(raw) <= 65536 else None
        detail = value.get("error") if isinstance(value, dict) else None
        code = (
            detail.get("code", detail.get("type"))
            if isinstance(detail, dict)
            else detail
        )
        if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,99}", code):
            suffix = "_" + code.upper()
    except (OSError, ValueError, TypeError):
        pass
    finally:
        error.close()
    return PlatformError(prefix + str(error.code) + suffix)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def personal_opener() -> urllib.request.OpenerDirector:
    proxy = os.environ.get("QF_PERSONAL_PROXY_URL", "").strip()
    if not proxy:
        return urllib.request.build_opener(NoRedirect())
    try:
        parsed = urllib.parse.urlsplit(proxy)
        valid = (
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost", "host.docker.internal"}
            and parsed.port is not None
            and parsed.username is None
            and parsed.password is None
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise PlatformError("PERSONAL_PROXY_INVALID")
    # A configured proxy failure must not silently retry a direct connection.
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}), NoRedirect()
    )


def fetch_json(
    url: str, *, form: dict[str, str] | None = None, token: str | None = None
) -> dict[str, Any]:
    if url not in {TOKEN_ENDPOINT, JWKS_ENDPOINT, RESOURCE + "/models"}:
        raise PlatformError("PERSONAL_ENDPOINT_REFUSED")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if form is not None:
        data = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with personal_opener().open(request, timeout=20) as response:
            raw = response.read(1000001)
        if len(raw) > 1000000:
            raise ValueError("bounded JSON required")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("object required")
        return value
    except urllib.error.HTTPError as error:
        # Do not log bearer credentials, OAuth codes or untrusted error prose.
        raise http_failure(error, "PERSONAL_AUTH_HTTP_") from error
    except (OSError, ValueError, TypeError) as error:
        raise PlatformError("PERSONAL_AUTH_RESPONSE_UNKNOWN") from error


def decode64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def verify_identity(
    encoded: str,
    client_id: str,
    *,
    nonce: str | None,
    expected_subject: str | None = None,
    jwks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        if len(encoded) > 32768:
            raise ValueError("bounded ID token required")
        header_part, claims_part, signature = encoded.split(".")
        header = json.loads(decode64(header_part))
        claims = json.loads(decode64(claims_part))
        if header.get("alg") != "RS256":
            raise ValueError("RS256 required")
        keys = (jwks if jwks is not None else fetch_json(JWKS_ENDPOINT))["keys"]
        key = next(
            x
            for x in keys
            if x.get("kid") == header.get("kid") and x.get("kty") == "RSA"
        )
        public = rsa.RSAPublicNumbers(
            int.from_bytes(decode64(key["e"])), int.from_bytes(decode64(key["n"]))
        ).public_key()
        public.verify(
            decode64(signature),
            (header_part + "." + claims_part).encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        audience = claims.get("aud")
        audiences = audience if isinstance(audience, list) else [audience]
        if (
            claims.get("iss") != ISSUER
            or client_id not in audiences
            or type(claims.get("exp")) not in (int, float)
            or claims["exp"] <= time.time()
            or not isinstance(claims.get("sub"), str)
            or not claims["sub"]
            or ("nbf" in claims and claims["nbf"] > time.time() + 30)
            or (
                nonce is not None
                and not hmac.compare_digest(str(claims.get("nonce", "")), nonce)
            )
            or (expected_subject is not None and claims["sub"] != expected_subject)
        ):
            raise ValueError("identity claims differ")
        return cast(dict[str, Any], claims)
    except (ValueError, TypeError, KeyError, StopIteration, AttributeError) as error:
        raise PlatformError("PERSONAL_ID_TOKEN_INVALID") from error
    except Exception as error:
        # cryptography InvalidSignature has no safe user-visible detail.
        raise PlatformError("PERSONAL_ID_TOKEN_INVALID") from error


def visible_models(value: dict[str, Any]) -> list[dict[str, str]]:
    rows = value.get("models")
    if not isinstance(rows, list):
        raise PlatformError("PERSONAL_MODEL_CATALOG_INVALID")
    result = [
        {"slug": x["slug"], "display_name": x.get("display_name", x["slug"])}
        for x in rows
        if isinstance(x, dict)
        and x.get("visibility") == "list"
        and isinstance(x.get("slug"), str)
        and isinstance(x.get("display_name", x["slug"]), str)
    ]
    if not result or len(result) > 200:
        raise PlatformError("PERSONAL_NO_AVAILABLE_MODEL")
    return result


class AccountStore:
    def __init__(self, directory: Path) -> None:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root = local_root(directory)

    def read(self, name: str) -> dict[str, Any]:
        path = self.root / name
        if path.is_symlink() or (path.exists() and path.stat().st_size > 1000000):
            raise PlatformError("PERSONAL_ACCOUNT_STORE_INVALID")
        if not path.exists():
            return {}
        data = path.read_bytes()
        if name == "credentials.enc":
            try:
                data = self.cipher().decrypt(data)
            except InvalidToken as error:
                raise PlatformError("PERSONAL_ACCOUNT_STORE_INVALID") from error
        value = json.loads(data)
        if not isinstance(value, dict):
            raise PlatformError("PERSONAL_ACCOUNT_STORE_INVALID")
        return value

    def cipher(self) -> Fernet:
        path = self.root / "account.key"
        if path.is_symlink():
            raise PlatformError("PERSONAL_ACCOUNT_STORE_INVALID")
        if not path.exists():
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(Fernet.generate_key())
            except FileExistsError:
                pass
        return Fernet(path.read_bytes())

    def write(self, name: str, value: dict[str, Any]) -> None:
        path = self.root / name
        temporary = self.root / (name + "." + uuid.uuid4().hex + ".pending")
        if path.is_symlink():
            raise PlatformError("PERSONAL_ACCOUNT_STORE_INVALID")
        data = canonical(value).encode()
        if name == "credentials.enc":
            data = self.cipher().encrypt(data)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def host_id(self) -> str:
        with writer_lock(self.root):
            value = self.read("host.json")
            if not value:
                value = {"ext_agent_host_id": "urn:uuid:" + str(uuid.uuid4())}
                self.write("host.json", value)
        return str(value["ext_agent_host_id"])

    def status(self) -> dict[str, Any]:
        value = self.read("credentials.enc")
        return {
            "connected": bool(
                value.get("subject")
                and value.get("access_token")
                and (
                    value.get("expires_at", 0) > time.time()
                    or value.get("refresh_token")
                )
                and PLAN_SCOPE in value.get("scopes", [])
                and value.get("refresh_state") not in {"UNKNOWN", "INFLIGHT"}
            ),
            "profile": digest([value.get("subject"), value.get("client_id")])
            if value
            else None,
        }

    def access_token(self) -> str:
        with writer_lock(self.root):
            value = self.read("credentials.enc")
            if not self.status()["connected"]:
                raise PlatformError("PERSONAL_AUTH_REQUIRED")
            if value.get("expires_at", 0) > time.time() + 60:
                return str(value["access_token"])
            if not value.get("refresh_token"):
                raise PlatformError("PERSONAL_AUTH_REQUIRED")
            value["refresh_state"] = "INFLIGHT"
            self.write("credentials.enc", value)
            try:
                refreshed = fetch_json(
                    TOKEN_ENDPOINT,
                    form={
                        "grant_type": "refresh_token",
                        "client_id": value["client_id"],
                        "refresh_token": value["refresh_token"],
                        "resource": RESOURCE,
                    },
                )
                identity = (
                    verify_identity(
                        refreshed["id_token"],
                        value["client_id"],
                        nonce=None,
                        expected_subject=value["subject"],
                    )
                    if refreshed.get("id_token")
                    else {"sub": value["subject"]}
                )
                value = token_record(
                    refreshed, value["client_id"], identity, prior=value
                )
                self.write("credentials.enc", value)
            except Exception as error:
                value["refresh_state"] = "UNKNOWN"
                self.write("credentials.enc", value)
                raise PlatformError("PERSONAL_REAUTH_REQUIRED") from error
            return str(value["access_token"])


def token_record(
    tokens: dict[str, Any],
    client_id: str,
    identity: dict[str, Any],
    *,
    prior: dict[str, Any] | None = None,
) -> dict[str, Any]:
    old = prior or {}
    scope = tokens.get("scope")
    scopes = scope.split() if isinstance(scope, str) else old.get("scopes", [])
    expiry = tokens.get("expires_in")
    if (
        not isinstance(tokens.get("access_token"), str)
        or not tokens["access_token"]
        or type(expiry) is not int
        or not 1 <= expiry <= 604800
        or tokens.get("token_type", "Bearer").lower() != "bearer"
    ):
        raise PlatformError("PERSONAL_TOKEN_RESPONSE_INVALID")
    return {
        "subject": identity["sub"],
        "client_id": client_id,
        "email": identity.get("email", old.get("email", "")),
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token", old.get("refresh_token")),
        "id_token": tokens.get("id_token", old.get("id_token")),
        "expires_at": time.time() + expiry,
        "scopes": scopes,
        "refresh_state": "READY",
    }


class Authorization:
    def __init__(self, store: AccountStore, redirect_uri: str) -> None:
        self.store = store
        self.redirect_uri = redirect_uri
        self.state = secrets.token_urlsafe(32)
        self.nonce = secrets.token_urlsafe(32)
        self.verifier = secrets.token_urlsafe(48)
        self.started = time.monotonic()
        self.client_id = store.read("registration.json").get("client_id", "")
        self.host_id = store.host_id()
        self.used = False

    def url(self) -> str:
        fields = {
            "client_id": self.client_id or "dynamic_agent_client",
            "ext_agent_host_id": self.host_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": "openid profile email offline_access resource.invoke "
            + PLAN_SCOPE,
            "resource": RESOURCE,
            "state": self.state,
            "nonce": self.nonce,
            "code_challenge_method": "S256",
            "code_challenge": base64.urlsafe_b64encode(
                hashlib.sha256(self.verifier.encode()).digest()
            )
            .decode()
            .rstrip("="),
        }
        if not self.client_id:
            fields["agent_name_hint"] = APP_NAME
        return AUTHORIZE + "?" + urllib.parse.urlencode(fields)

    def complete(self, query: dict[str, str]) -> None:
        if (
            self.used
            or time.monotonic() - self.started > 600
            or not hmac.compare_digest(query.get("state", ""), self.state)
        ):
            raise PlatformError("PERSONAL_OAUTH_STATE_INVALID")
        self.used = True
        if query.get("error"):
            raise PlatformError("PERSONAL_OAUTH_DENIED")
        issued = query.get("client_id", self.client_id)
        if (
            not issued
            or issued == "dynamic_agent_client"
            or (self.client_id and issued != self.client_id)
            or len(issued) > 256
        ):
            raise PlatformError("PERSONAL_REGISTRATION_INVALID")
        if not query.get("code"):
            raise PlatformError("PERSONAL_OAUTH_CODE_MISSING")
        self.store.write("registration.json", {"client_id": issued})
        tokens = fetch_json(
            TOKEN_ENDPOINT,
            form={
                "grant_type": "authorization_code",
                "client_id": issued,
                "code": query["code"],
                "code_verifier": self.verifier,
                "redirect_uri": self.redirect_uri,
                "resource": RESOURCE,
            },
        )
        prior = self.store.read("credentials.enc")
        identity = verify_identity(
            tokens["id_token"],
            issued,
            nonce=self.nonce,
            expected_subject=prior.get("subject"),
        )
        record = token_record(tokens, issued, identity)
        self.store.write("credentials.enc", record)
        if PLAN_SCOPE not in record["scopes"]:
            raise PlatformError("PERSONAL_PLAN_PERMISSION_NOT_GRANTED")
        models = visible_models(
            fetch_json(RESOURCE + "/models", token=record["access_token"])
        )
        self.store.write("models.json", {"models": models})
        settings = self.store.read("settings.json")
        self.store.write(
            "settings.json",
            {
                "model": settings.get("model")
                if settings.get("model") in {x["slug"] for x in models}
                else models[0]["slug"]
            },
        )
