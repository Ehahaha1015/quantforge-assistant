import json
import threading
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from qf_platform.server import make_server


def test_forward_read_is_isolated_and_review_requires_explicit_local_mode(tmp_path):
    server = make_server(tmp_path, port=0, chart_assistant=True, chart_only=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    opener = build_opener(ProxyHandler({}))
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with opener.open(base + "/api/chart-forward") as response:
            status = json.load(response)
        assert status["status"] == "DISABLED" and status["model_calls"] == 0
        path = base + "/api/chart-analyses/chart-" + "a" * 32 + "/review"
        body = json.dumps({"action": "review"}).encode()
        for headers, expected in (
            ({"Content-Type": "application/json"}, 403),
            ({"Content-Type": "application/json", "X-QF-Mode": "RESEARCH"}, 404),
        ):
            try:
                opener.open(Request(path, data=body, headers=headers))
                raise AssertionError("Nonexistent or unauthorized report accepted")
            except HTTPError as error:
                assert error.code == expected
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
