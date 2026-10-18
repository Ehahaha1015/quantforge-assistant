"""Bounded ASGI transport policy and in-process, payload-free HTTP observations."""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections import deque
from typing import Any

from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .chart_reports import MAX_REQUEST_BYTES

ALLOWED_ORIGINS = frozenset(
    f"http://127.0.0.1:{port}" for port in (4173, 4179, 4180, 4181, 4182)
)


class HttpObservations:
    def __init__(self) -> None:
        self.rows: deque[dict[str, Any]] = deque(maxlen=1024)
        self.lock = threading.Lock()

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "schema_version": "chart-http-observations-v1",
                "scope": "LAST_1024_REQUESTS_THIS_PROCESS_NO_PAYLOADS",
                "items": list(self.rows),
            }

    def record(self, row: dict[str, Any]) -> None:
        with self.lock:
            self.rows.append(row)


class LocalChartMiddleware:
    def __init__(
        self, app: ASGIApp, *, port: int, observations: HttpObservations
    ) -> None:
        self.app, self.port, self.observations = app, port, observations

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started, trace_id = time.perf_counter(), uuid.uuid4().hex
        headers = dict(scope["headers"])
        origin = headers.get(b"origin", b"").decode("latin1")
        status = 500

        async def observed_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                extra = [
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-request-id", trace_id.encode()),
                ]
                if origin in ALLOWED_ORIGINS:
                    extra += [
                        (b"access-control-allow-origin", origin.encode()),
                        (b"vary", b"Origin"),
                    ]
                message["headers"] = [*message.get("headers", []), *extra]
            await send(message)

        async def refuse(code: int, error: str) -> None:
            await JSONResponse({"error": error}, status_code=code)(
                scope, receive, observed_send
            )

        try:
            critical = {
                b"host",
                b"origin",
                b"content-length",
                b"content-type",
                b"x-qf-mode",
                b"idempotency-key",
            }
            if any(sum(k == key for k, _ in scope["headers"]) > 1 for key in critical):
                await refuse(409, "DUPLICATE_CONTROL_HEADER")
                return
            if headers.get(b"host") != f"127.0.0.1:{self.port}".encode() or (
                origin and origin not in ALLOWED_ORIGINS
            ):
                await refuse(403, "LOCAL_ORIGIN_REQUIRED")
                return
            path, method = scope["path"], scope["method"]
            if method == "OPTIONS":
                await Response(
                    status_code=204,
                    headers={
                        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                        "Access-Control-Allow-Headers": (
                            "Content-Type, X-QF-Mode, Idempotency-Key"
                        ),
                    },
                )(scope, receive, observed_send)
                return
            if method == "POST":
                if not path.startswith("/api/chart-"):
                    await refuse(403, "SCREENSHOT_PRODUCT_ONLY")
                    return
                required = (
                    b"CONFIGURATION" if path == "/api/chart-settings" else b"RESEARCH"
                )
                if headers.get(b"x-qf-mode") != required:
                    await refuse(403, "EXPLICIT_LOCAL_MODE_REQUIRED")
                    return
                limit = MAX_REQUEST_BYTES if path == "/api/chart-analyses" else 65536
                try:
                    length = int(headers.get(b"content-length", b"0"))
                except ValueError:
                    length = 0
                if (
                    not 0 < length <= limit
                    or b"transfer-encoding" in headers
                    or headers.get(b"content-type") != b"application/json"
                ):
                    await refuse(409, "BOUNDED_JSON_REQUIRED")
                    return
                body = bytearray()
                deadline = time.monotonic() + 10
                while True:
                    try:
                        part = await asyncio.wait_for(
                            receive(), timeout=max(0, deadline - time.monotonic())
                        )
                    except TimeoutError:
                        await refuse(409, "BOUNDED_JSON_REQUIRED")
                        return
                    if part["type"] != "http.request":
                        return
                    body.extend(part.get("body", b""))
                    if len(body) > length:
                        await refuse(409, "BOUNDED_JSON_REQUIRED")
                        return
                    if not part.get("more_body"):
                        break
                if len(body) != length:
                    await refuse(409, "BOUNDED_JSON_REQUIRED")
                    return

                async def replay() -> Message:
                    return {
                        "type": "http.request",
                        "body": bytes(body),
                        "more_body": False,
                    }

                await self.app(scope, replay, observed_send)
            else:
                await self.app(scope, receive, observed_send)
        finally:
            self.observations.record(
                {
                    "trace_id": trace_id,
                    "method": scope["method"],
                    "status": status,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                }
            )
