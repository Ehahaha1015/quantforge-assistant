"""Run the synthetic showcase on loopback without Docker or provider credentials."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
API_PORT, WEB_PORT = 4188, 4173


def require_free_ports() -> None:
    for port in (API_PORT, WEB_PORT):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError as exc:
                raise RuntimeError(
                    f"Port {port} is in use. Stop its service first."
                ) from exc


def wait_ready(
    process: subprocess.Popen[bytes], url: str, *, demo: bool = False
) -> None:
    opener = build_opener(ProxyHandler({}))
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Service exited ({process.returncode}); see logs.")
        try:
            with opener.open(url, timeout=2) as response:
                if response.status == 200 and (
                    not demo or json.load(response).get("demo") is True
                ):
                    return
        except (URLError, OSError, ValueError):
            pass
        time.sleep(0.2)
    raise RuntimeError(f"Service did not become ready: {url}")


def stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.send_signal(
            signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT
        )
        process.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--install", action="store_true", help="Install locked dependencies first"
    )
    args = parser.parse_args()
    processes: list[subprocess.Popen[bytes]] = []
    try:
        require_free_ports()
        environment = os.environ.copy()
        for key, folder in {
            "TEMP": "temp",
            "TMP": "temp",
            "UV_CACHE_DIR": "uv",
            "npm_config_cache": "npm",
        }.items():
            directory = ROOT / "local-tools/cache" / folder
            directory.mkdir(parents=True, exist_ok=True)
            environment[key] = str(directory)
        # Ignore an inherited live API target; this command always serves the demo.
        environment["VITE_QF_BINDINGS_API"] = "/gateway"
        environment["PYTHONUNBUFFERED"] = "1"
        if args.install:
            for command in (
                ["uv", "sync", "--project", "qf-platform", "--frozen"],
                [
                    "npm.cmd" if os.name == "nt" else "npm",
                    "ci",
                    "--prefix",
                    "quantforge-ui",
                ],
            ):
                subprocess.run(command, cwd=ROOT, env=environment, check=True)
        python = (
            ROOT
            / "qf-platform/.venv"
            / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
        vite = ROOT / "quantforge-ui/node_modules/vite/bin/vite.js"
        node = shutil.which("node")
        if not python.is_file() or not vite.is_file() or node is None:
            raise RuntimeError(
                "Dependencies missing. Install Python >=3.12, Node 24 and uv; rerun with --install."
            )
        logs = ROOT / "local-smoke/local-showcase"
        logs.mkdir(parents=True, exist_ok=True)
        print(f"Synthetic offline demo; no real model calls. Logs: {logs}", flush=True)
        commands = [
            (
                "api",
                ROOT,
                [
                    str(python),
                    "-m",
                    "qf_platform.chart_demo",
                    "--root",
                    str(ROOT / "local-smoke/local-showcase-data"),
                    "--port",
                    str(API_PORT),
                ],
            ),
            (
                "web",
                ROOT / "quantforge-ui",
                [
                    node,
                    str(vite),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(WEB_PORT),
                    "--strictPort",
                ],
            ),
        ]
        for name, cwd, command in commands:
            with (logs / f"{name}.log").open("ab") as output:
                process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=environment,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                    if os.name == "nt"
                    else 0,
                )
            processes.append(process)
            wait_ready(
                process,
                f"http://127.0.0.1:{API_PORT}/api/health"
                if name == "api"
                else f"http://127.0.0.1:{WEB_PORT}",
                demo=name == "api",
            )
        # Verify the same-origin gateway, not just the two independent servers.
        wait_ready(
            processes[-1], f"http://127.0.0.1:{WEB_PORT}/gateway/api/health", demo=True
        )
        print(
            f"Ready: http://127.0.0.1:{WEB_PORT}/#chart-reports\nPress Ctrl+C to stop both services; demo records are retained.",
            flush=True,
        )
        while True:
            for process in processes:
                if process.poll() is not None:
                    raise RuntimeError(
                        f"Service exited ({process.returncode}); see {logs}."
                    )
            time.sleep(0.5)
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Showcase startup failed: {exc}", file=sys.stderr)
        return 1
    finally:
        for process in reversed(processes):
            stop(process)


if __name__ == "__main__":
    raise SystemExit(main())
