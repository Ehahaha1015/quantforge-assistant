"""Reproducible subprocess faults and real HTTP benchmarks, exclusively offline."""

from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

from .chart_analysis import ChartAnalysisService
from .chart_demo import (
    FIXTURE_KEY,
    OfflineChartRuntime,
    check_demo_root,
    sample_request,
)
from .storage import canonical

STAGES = (
    "BEFORE_RESERVATION",
    "AFTER_RESERVATION",
    "BEFORE_SEND",
    "DURING_SEND",
    "AFTER_RESPONSE",
    "BEFORE_RESULT_COMMIT",
    "AFTER_RESULT_COMMIT",
)


def app_at(root: Path) -> ChartAnalysisService:
    check_demo_root(root, initialize=True)
    app = ChartAnalysisService(root, runtime=OfflineChartRuntime(root))
    if not app.status()["configured"]:
        app.configure({"api_key": FIXTURE_KEY, "max_cost_cny": "10.00"})
    return app


def calls(root: Path) -> int:
    path = root / "offline-calls.jsonl"
    return len(path.read_text(encoding="utf-8").splitlines()) if path.exists() else 0


def fault_checks(root: Path) -> list[dict[str, Any]]:
    rows = []
    for stage in STAGES:
        directory = root / stage.lower()
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "qf_platform.showcase_checks",
                "crash-worker",
                "--root",
                str(directory),
                "--stage",
                stage,
            ],
            capture_output=True,
            timeout=60,
        )
        (root / (stage.lower() + ".log")).write_bytes(result.stdout + result.stderr)
        if result.returncode != 73:
            raise ValueError(
                f"Fault did not reach checkpoint {stage}: {result.returncode}"
            )
        before = calls(directory)
        restored = app_at(directory)
        restored.tick()
        restored.tick()
        items = restored.list()["items"]
        expected = (
            "SUCCEEDED"
            if stage in {"BEFORE_RESERVATION", "AFTER_RESULT_COMMIT"}
            else "UNKNOWN"
        )
        assert len(items) == 1 and items[0]["status"] == expected
        assert calls(directory) == (1 if expected == "SUCCEEDED" else before)
        assert restored.status()["calls_used"] == 1
        rows.append(
            {
                "stage": stage,
                "process_exit": result.returncode,
                "recovered_status": expected,
                "offline_sends_before_recovery": before,
                "offline_sends_after_recovery": calls(directory),
                "reservations_after_recovery": restored.status()["calls_used"],
                "passed": True,
            }
        )
    return rows


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))], 3)


def benchmark(root: Path) -> dict[str, Any]:
    app = app_at(root)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    if sys.platform == "win32":
        creation = subprocess.CREATE_NO_WINDOW
    else:
        creation = 0
    with (root / "http-server.log").open("wb") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "qf_platform.showcase_checks",
                "http-server",
                "--root",
                str(root),
                "--port",
                str(port),
            ],
            stdout=log,
            stderr=log,
            creationflags=creation,
        )
        try:
            opener = build_opener(ProxyHandler({}))
            for _ in range(100):
                try:
                    with opener.open(f"http://127.0.0.1:{port}/api/health", timeout=1):
                        break
                except (OSError, URLError):
                    if process.poll() is not None:
                        raise ValueError("Benchmark server exited") from None
                    time.sleep(0.1)
            else:
                raise ValueError("Benchmark server unavailable")
            body = canonical(sample_request()).encode()

            def submit(index: int) -> tuple[float, str]:
                request = Request(
                    f"http://127.0.0.1:{port}/api/chart-analyses",
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-QF-Mode": "RESEARCH",
                        "Idempotency-Key": "same-100-attempts",
                    },
                )
                began = time.perf_counter()
                with build_opener(ProxyHandler({})).open(
                    request, timeout=30
                ) as response:
                    value = json.load(response)
                    assert response.status == 200 and response.headers["X-Request-Id"]
                return (time.perf_counter() - began) * 1000, value["id"]

            started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=16) as pool:
                results = list(pool.map(submit, range(100)))
            wall = time.perf_counter() - started
            assert (
                len({job for _, job in results}) == 1 and len(app.list()["items"]) == 1
            )
            began = time.perf_counter()
            app.tick()
            processing = time.perf_counter() - began
            assert calls(root) == 1 and app.status()["calls_used"] == 1
            assert app.list()["items"][0]["status"] == "SUCCEEDED"
            return {
                "transport": "REAL_LOOPBACK_HTTP_UVICORN",
                "attempts": 100,
                "concurrency": 16,
                "unique_jobs": 1,
                "offline_sends": 1,
                "reservations": 1,
                "request_bytes": len(body),
                "acceptance_p50_ms": percentile([x for x, _ in results], 0.5),
                "acceptance_p95_ms": percentile([x for x, _ in results], 0.95),
                "wall_seconds": wall,
                "single_job_offline_processing_seconds": processing,
                "scope": (
                    "SAME_IDEMPOTENCY_KEY_SMALL_SYNTHETIC_IMAGE_"
                    "NOT_DISTINCT_UPLOAD_THROUGHPUT_OR_REAL_MODEL_LATENCY"
                ),
            }
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("verify", "crash-worker", "http-server"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--stage", choices=STAGES)
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    if args.action == "crash-worker":
        app = app_at(args.root)
        app.create(sample_request(), "process-fault")
        app.runtime = OfflineChartRuntime(args.root, crash_at=args.stage)
        app.tick()
    elif args.action == "http-server":
        import uvicorn

        from .chart_api import create_app

        uvicorn.run(
            create_app(
                args.root,
                port=args.port,
                runtime=OfflineChartRuntime(args.root),
                demo=True,
            ),
            host="127.0.0.1",
            port=args.port,
            access_log=False,
        )
    else:
        if args.root.exists():
            raise ValueError(
                "A verification run requires a new root, never overwrite evidence"
            )
        args.root.mkdir(parents=True)
        result = {
            "schema_version": "showcase-fault-http-v1",
            "at": datetime.now(UTC).isoformat(),
            "environment": {
                "python": sys.version,
                "platform": platform.platform(),
                "cpu_count": os.cpu_count(),
            },
            "faults": fault_checks(args.root),
            "benchmark": benchmark(args.root / "http-benchmark"),
            "passed": True,
            "real_model_calls": 0,
        }
        with (args.root / "receipt.json").open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
        print(canonical(result))


if __name__ == "__main__":
    main()
