"""Local browser OAuth entry point. Never prints codes, tokens or authorization URLs."""

from __future__ import annotations

import argparse
import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .personal_auth import AccountStore, Authorization
from .storage import PlatformError


def launch_browser(url: str) -> bool:
    try:
        controller = webbrowser.get()
        if isinstance(controller, webbrowser.GenericBrowser):
            # BROWSER command templates use GenericBrowser, which waits for
            # Chrome to exit. The callback listener must start immediately.
            controller = webbrowser.BackgroundBrowser(
                [controller.name, *controller.args]
            )
        return controller.open(url, new=1)
    except (webbrowser.Error, OSError):
        return False


def callback_server(
    store: AccountStore,
) -> tuple[ThreadingHTTPServer, Authorization, dict[str, str]]:
    result: dict[str, str] = {}
    callback: Authorization | None = None
    completion_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        # Chrome may pre-open an idle socket before sending the real callback.
        timeout = 5

        def log_message(self, format: str, *values: Any) -> None:
            return

        def do_GET(self) -> None:
            if urlsplit(self.path).path != "/auth/callback":
                self.send_error(404)
                return
            try:
                if self.headers.get("Host") != f"127.0.0.1:{server.server_port}":
                    raise ValueError("loopback callback required")
                fields = parse_qs(urlsplit(self.path).query, strict_parsing=True)
                if any(len(values) != 1 for values in fields.values()):
                    raise ValueError("single callback values required")
                values = {key: fields[key][0] for key in fields}
                assert callback is not None
                with completion_lock:
                    callback.complete(values)
                result["state"] = "CONNECTED"
                body = "ChatGPT account connected. Return to QQ."
            except (ValueError, OSError, KeyError, TypeError) as error:
                if (
                    isinstance(error, PlatformError)
                    and str(error) == "PERSONAL_OAUTH_STATE_INVALID"
                ):
                    self.send_error(400, "Invalid OAuth state")
                    return
                result["state"] = (
                    str(error)
                    if isinstance(error, PlatformError)
                    else "PERSONAL_LOGIN_FAILED"
                )
                body = "Authorization did not complete. Review the local status."
                if result["state"].endswith("_UNSUPPORTED_COUNTRY_REGION_TERRITORY"):
                    body = (
                        "OpenAI rejected the token request: "
                        "unsupported_country_region_territory. "
                        "The account is not connected. "
                        "Return to the project for details."
                    )
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body.encode())))
            self.end_headers()
            try:
                self.wfile.write(body.encode())
            except OSError:
                # A closed browser must not obscure the completed OAuth result.
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    # Finish an accepted exchange before process exit, including at the deadline.
    server.daemon_threads = False
    server.timeout = 1
    callback = Authorization(
        store, f"http://127.0.0.1:{server.server_port}/auth/callback"
    )
    return server, callback, result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-dir", type=Path, required=True)
    args = parser.parse_args()
    store = AccountStore(args.account_dir)
    server, callback, result = callback_server(store)
    print(
        "Continue with ChatGPT: complete sign-in and plan consent in the browser.",
        flush=True,
    )
    try:
        if not launch_browser(callback.url()):
            result["state"] = "PERSONAL_BROWSER_OPEN_FAILED"
        end = time.monotonic() + 600
        while not result and time.monotonic() < end:
            server.handle_request()
    finally:
        server.server_close()
    status = {
        "state": result.get("state", "AUTHORIZATION_TIMEOUT"),
        "connected": store.status()["connected"],
    }
    store.write("login-status.json", status)
    print(json.dumps(status), flush=True)
    if result.get("state") != "CONNECTED":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
