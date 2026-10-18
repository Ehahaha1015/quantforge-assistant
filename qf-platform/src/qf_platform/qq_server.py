"""Private OneBot event receiver and transport pump, separate from model execution."""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from quantforge.product.storage import local_root, writer_lock

from .qq_bridge import QQBridge
from .qq_protocol import QQConfig, verify_signature
from .storage import PlatformError, canonical


def make_server(
    bridge: QQBridge, port: int = 4190, host: str = "127.0.0.1"
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        timeout = 5

        def log_message(self, format: str, *args: Any) -> None:
            return

        def answer(self, code: int, value: dict[str, Any]) -> None:
            data = canonical(value).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(data)
            except OSError:
                pass

        def do_GET(self) -> None:
            if self.path != "/health":
                self.answer(404, {})
                return
            self.answer(
                200,
                {
                    "service": "quantforge-qq-bridge",
                    "configured": bridge.config.active,
                    "connected": bridge.online,
                    "model_worker": False,
                },
            )

        def do_POST(self) -> None:
            if urlsplit(self.path).path != "/events" or urlsplit(self.path).query:
                self.answer(404, {})
                return
            try:
                raw = self.read_body()
                verify_signature(
                    raw, self.headers.get("X-Signature", ""), bridge.config.token
                )
                if self.headers.get("X-Self-ID") != bridge.config.bot_id:
                    self.answer(403, {})
                    return
                event = json.loads(raw)
                if not isinstance(event, dict):
                    raise ValueError("object required")
                bridge.accept(event)
                # Empty quick-action response: all sends go through the durable outbox.
                self.answer(200, {})
            except PlatformError as error:
                self.answer(
                    403
                    if str(error) == "QQ_EVENT_UNAUTHENTICATED"
                    else 413
                    if str(error) == "QQ_EVENT_TOO_LARGE"
                    else 409,
                    {"error": str(error)},
                )
            except (ValueError, OSError, KeyError, TypeError):
                self.answer(400, {"error": "QQ_EVENT_INVALID"})

        def read_body(self) -> bytes:
            lengths = self.headers.get_all("Content-Length", [])
            transfer = self.headers.get_all("Transfer-Encoding", [])
            if transfer:
                if lengths or transfer != ["chunked"]:
                    raise ValueError("ambiguous framing")
                chunks: list[bytes] = []
                total = 0
                deadline = time.monotonic() + 5
                for _ in range(256):
                    line = self.rfile.readline(80)
                    if not re.fullmatch(rb"[0-9a-fA-F]{1,6}\r\n", line):
                        raise ValueError("invalid chunk framing")
                    size = int(line.strip(), 16)
                    total += size
                    if total > 196608:
                        raise PlatformError("QQ_EVENT_TOO_LARGE")
                    if time.monotonic() > deadline:
                        raise ValueError("body read deadline")
                    chunk = self.rfile.read(size)
                    if len(chunk) != size or self.rfile.read(2) != b"\r\n":
                        raise ValueError("incomplete chunk")
                    if not size:
                        if not total:
                            raise ValueError("empty body")
                        return b"".join(chunks)
                    chunks.append(chunk)
                raise ValueError("too many chunks")
            if len(lengths) != 1:
                raise ValueError("bounded body required")
            length = int(lengths[0])
            if not 1 <= length <= 196608:
                raise PlatformError("QQ_EVENT_TOO_LARGE")
            data = self.rfile.read(length)
            if len(data) != length:
                raise ValueError("incomplete body")
            return data

    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--host", choices=("127.0.0.1", "0.0.0.0"), default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4190)
    args = parser.parse_args()
    directory = local_root(args.root) / "qq-bridge"
    directory.mkdir(exist_ok=True)
    with writer_lock(local_root(directory)):
        bridge = QQBridge(args.root, QQConfig.load(args.config))
        server = make_server(bridge, args.port, args.host)
        stopped = threading.Event()

        def pump() -> None:
            last_probe = 0.0
            while not stopped.is_set():
                try:
                    if time.monotonic() - last_probe > 15:
                        bridge.probe()
                        last_probe = time.monotonic()
                    bridge.process_one()
                    bridge.collect_results()
                    bridge.deliver_one()
                except (ValueError, OSError, KeyError, TypeError):
                    # Keep diagnostics bounded and free of signed image URLs or tokens.
                    bridge.online = False
                stopped.wait(1)

        worker = threading.Thread(target=pump, name="qq-transport", daemon=True)
        worker.start()
        print(
            "QQ private transport listening; model worker unchanged",
            flush=True,
        )
        try:
            server.serve_forever(poll_interval=0.5)
        finally:
            stopped.set()
            server.server_close()
            worker.join(timeout=3)


if __name__ == "__main__":
    main()
