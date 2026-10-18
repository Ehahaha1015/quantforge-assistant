"""Loopback console with explicit configuration, research and money domains."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sqlite3
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .agent_tasks import AgentTaskService
from .bindings import BindingRegistry
from .brokers import BrokerService
from .chart_analysis import ChartAnalysisService
from .chart_reports import MAX_REQUEST_BYTES, markdown
from .research_archive import ResearchArchive
from .research_room import RoomService
from .service import PlatformService
from .storage import PlatformError, canonical
from .supervisor import WorkerSupervisor
from .tasks import TaskService

ALLOWED_ORIGINS = frozenset(
    {
        "http://127.0.0.1:4173",
        "http://127.0.0.1:4179",
        "http://127.0.0.1:4180",
        "http://127.0.0.1:4181",
        "http://127.0.0.1:4182",
    }
)

# A job id that is malformed or absent is a missing resource, not a conflict.
# Everything else the chart store refuses (channel not configured, report not
# ready, budget, idempotency conflict) really is a conflicting state.
ABSENT_CHART = frozenset({"CHART_ID_INVALID", "CHART_NOT_FOUND"})


def chart_refusal_status(error: PlatformError) -> int:
    return 404 if str(error) in ABSENT_CHART else 409


class PlatformServer(ThreadingHTTPServer):
    daemon_threads = True
    scheduler_stop: threading.Event
    scheduler: threading.Thread
    supervisor: WorkerSupervisor | None
    task_readers: tuple[TaskService, ...]

    def server_close(self) -> None:
        self.scheduler_stop.set()
        self.scheduler.join(timeout=3)
        if self.supervisor is not None:
            self.supervisor.close()
        for reader in self.task_readers:
            reader.close_view_cache()
        super().server_close()


def make_server(
    root: Path,
    port: int = 4188,
    *,
    embedded_scheduler: bool = False,
    host: str = "127.0.0.1",
    offline_demo: bool = False,
    chart_assistant: bool = False,
    chart_only: bool = False,
) -> PlatformServer:
    if host not in {"127.0.0.1", "0.0.0.0"}:
        raise PlatformError("SERVER_LISTEN_HOST_INVALID")
    if chart_only and not chart_assistant:
        raise PlatformError("CHART_ONLY_REQUIRES_CHART_ASSISTANT")
    service = PlatformService(root)
    bindings = BindingRegistry(root)
    room = RoomService(root, bindings, external_worker=not embedded_scheduler)
    brokers = BrokerService(root, bindings)
    tasks = TaskService(root, cache_views=True)
    agent_tasks = AgentTaskService(root, cache_views=True)
    archive = ResearchArchive()
    charts = ChartAnalysisService(root) if chart_assistant else None
    from .chart_forward import DailyForward

    forward = DailyForward(root) if charts is not None else None
    streams = threading.BoundedSemaphore(8)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        # Applied by StreamRequestHandler.setup() before rfile/wfile are built
        # from the socket. Setting a timeout on the socket afterwards leaves the
        # buffered file objects in the state CPython documents as inconsistent,
        # which resets connections instead of answering them.
        timeout = 10
        server: ThreadingHTTPServer

        def _task_view(self, target: TaskService, task_id: str) -> dict[str, Any]:
            params = parse_qs(urlsplit(self.path).query, strict_parsing=True)
            if params not in ({}, {"audit": ["full"]}):
                raise PlatformError("TASK_VIEW_QUERY_INVALID")
            return target.view(task_id, deep_audit=bool(params))

        def log_message(self, format: str, *args: Any) -> None:
            # No request bodies or credentials go to access logs.
            return

        def _task_event_page(self, target: TaskService, task_id: str) -> dict[str, Any]:
            params = parse_qs(urlsplit(self.path).query, strict_parsing=True)
            if set(params) - {
                "root_id",
                "head_seq",
                "head_hash",
                "after",
                "anchor",
                "limit",
            } or any(len(value) != 1 for value in params.values()):
                raise PlatformError("TASK_PAGE_QUERY_INVALID")
            return target.event_page(
                task_id,
                root_id=params["root_id"][0],
                head_seq=int(params["head_seq"][0]),
                head_hash=params["head_hash"][0],
                after=int(params.get("after", ["0"])[0]),
                anchor=params.get("anchor", [None])[0],
                limit=int(params.get("limit", ["25"])[0]),
            )

        def _allowed(self) -> bool:
            expected = f"127.0.0.1:{self.server.server_port}"
            return self.headers.get("Host") == expected and (
                self.headers.get("Origin") is None
                or self.headers["Origin"] in ALLOWED_ORIGINS
            )

        def _headers(self) -> None:
            origin = self.headers.get("Origin")
            if origin in ALLOWED_ORIGINS:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")

        def _json(self, status: int, value: dict[str, Any]) -> None:
            raw = canonical(value).encode()
            self.send_response(status)
            self._headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                self.wfile.write(raw)
            except OSError:
                pass

        def do_OPTIONS(self) -> None:
            if not self._allowed():
                self._json(403, {"error": "LOCAL_ORIGIN_REQUIRED"})
                return
            self.send_response(204)
            self._headers()
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Content-Type, X-QF-Mode, Idempotency-Key",
            )
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:
            if not self._allowed():
                self._json(403, {"error": "LOCAL_ORIGIN_REQUIRED"})
                return
            path = urlsplit(self.path).path
            if (
                chart_only
                and path != "/api/health"
                and not path.startswith("/api/chart-")
            ):
                self._json(404, {"error": "ROUTE_NOT_FOUND"})
                return
            if charts is not None and path.startswith("/api/chart-"):
                try:
                    parts = path.split("/")
                    if path == "/api/chart-settings":
                        self._json(200, charts.status())
                    elif path == "/api/chart-forward":
                        assert forward is not None
                        self._json(200, forward.status())
                    elif path == "/api/chart-analyses":
                        self._json(200, charts.list())
                    elif len(parts) in {4, 5} and parts[2] == "chart-analyses":
                        if len(parts) == 4:
                            self._json(200, charts.get(parts[3]))
                        elif parts[4] == "observation":
                            from .chart_followup import observe

                            self._json(
                                200, observe(charts.get(parts[3]), charts.market_data)
                            )
                        elif parts[4] in {"image", "report.md"}:
                            if parts[4] == "image":
                                raw, mime = charts.image(parts[3])
                            else:
                                value = charts.get(parts[3])
                                if value["report"] is None:
                                    raise PlatformError("CHART_REPORT_NOT_READY")
                                raw, mime = (
                                    markdown(value).encode(),
                                    "text/markdown; charset=utf-8",
                                )
                            self.send_response(200)
                            self._headers()
                            self.send_header("Content-Type", mime)
                            self.send_header("Content-Length", str(len(raw)))
                            if parts[4] == "report.md":
                                self.send_header(
                                    "Content-Disposition",
                                    f'attachment; filename="{parts[3]}.md"',
                                )
                            self.send_header("Connection", "close")
                            self.end_headers()
                            self.close_connection = True
                            self.wfile.write(raw)
                        else:
                            self._json(404, {"error": "ROUTE_NOT_FOUND"})
                    else:
                        self._json(404, {"error": "ROUTE_NOT_FOUND"})
                except PlatformError as error:
                    self._json(chart_refusal_status(error), {"error": str(error)})
                except (ValueError, KeyError, OSError, sqlite3.Error):
                    self._json(409, {"error": "CHART_OR_REPORT_UNAVAILABLE"})
                return
            if path == "/api/health":
                self._json(
                    200,
                    {
                        "status": "OK",
                        "service": "quantforge-chart-assistant"
                        if chart_assistant
                        else "quantforge-platform",
                        "schema_version": "qf-health-v1",
                        "observed_at": datetime.now(UTC).isoformat(),
                        "mode": "SIMULATION",
                        "real_transport": "LOCKED",
                        # task_mode describes what this deployment actually
                        # serves. A production chart-only container must not
                        # report OFFLINE_DEMO just because the flag that used
                        # to close the legacy routes was reused for it.
                        "task_mode": "OFFLINE_DEMO"
                        if offline_demo
                        else "CHART_ONLY"
                        if chart_only
                        else "RESEARCH",
                        "agent_task_mode": "DISABLED"
                        if offline_demo or chart_only
                        else "REAL_RESEARCH",
                        "deployment_profile": "SCREENSHOT_ASSISTANT"
                        if chart_assistant
                        else "OFFLINE_DEMO"
                        if offline_demo
                        else "LOCAL",
                        "worker_mode": "EMBEDDED_TEST_COMPATIBILITY"
                        if embedded_scheduler
                        else "EXTERNAL_PROCESSES",
                        "workers": server.supervisor.snapshot()
                        if server.supervisor is not None
                        else {},
                        "runtime_id": hashlib.sha256(
                            str(root.resolve()).encode()
                        ).hexdigest()[:24],
                    },
                )
            elif path == "/api/research-archive" or path.startswith(
                "/api/research-archive/"
            ):
                try:
                    parts = path.split("/")
                    params = parse_qs(urlsplit(self.path).query, strict_parsing=True)
                    if set(params) - {"asset", "scenario"} or any(
                        len(value) != 1 for value in params.values()
                    ):
                        raise ValueError("ARCHIVE_QUERY_INVALID")
                    if path == "/api/research-archive" and not params:
                        result = archive.list()
                    elif len(parts) == 4:
                        result = archive.get(
                            parts[3],
                            asset=params.get("asset", [None])[0],
                            scenario=params.get("scenario", [None])[0],
                        )
                    elif len(parts) == 6 and parts[4] == "artifacts" and not params:
                        result = archive.artifact(parts[3], parts[5])
                    else:
                        raise ValueError("ARCHIVE_ROUTE_INVALID")
                    self._json(200, result)
                except (ValueError, TypeError, KeyError, OSError, sqlite3.Error):
                    self._json(409, {"error": "RESEARCH_ARCHIVE_UNAVAILABLE"})
            elif path == "/api/agent-tasks" or path.startswith("/api/agent-tasks/"):
                try:
                    parts = path.split("/")
                    if path == "/api/agent-tasks":
                        result = agent_tasks.list()
                    elif path == "/api/agent-tasks/capabilities":
                        result = agent_tasks.capabilities()
                    elif len(parts) == 4:
                        result = agent_tasks.get(parts[3])
                    elif len(parts) == 5 and parts[4] == "view":
                        result = self._task_view(agent_tasks, parts[3])
                    elif len(parts) == 5 and parts[4] == "events":
                        result = self._task_event_page(agent_tasks, parts[3])
                    elif len(parts) == 5 and parts[4] == "report":
                        task = agent_tasks.get(parts[3])
                        if task["report"] is None:
                            raise PlatformError("TASK_REPORT_NOT_READY")
                        result = {
                            "schema_version": "qf-agent-task-report-v2",
                            "task_id": task["id"],
                            "mode": "REAL_RESEARCH",
                            "paid_model_calls": task["paid_model_calls"],
                            "model_call": task["model_call"],
                            "report": task["report"],
                            "live_authority": "NONE",
                        }
                    elif len(parts) == 6 and parts[4] == "evidence":
                        result = agent_tasks.evidence(parts[3], parts[5])
                    else:
                        raise PlatformError("AGENT_TASK_ROUTE_INVALID")
                    self._json(200, result)
                except (ValueError, TypeError, KeyError, OSError, sqlite3.Error):
                    self._json(409, {"error": "REAL_AGENT_TASK_UNAVAILABLE"})
            elif path == "/api/knowledge/search":
                try:
                    from .knowledge import query_corpus

                    params = parse_qs(urlsplit(self.path).query, strict_parsing=True)
                    if set(params) != {"q"} or len(params["q"]) != 1:
                        raise ValueError("bounded query only")
                    self._json(
                        200, query_corpus(params["q"][0], as_of=datetime.now(UTC))
                    )
                except (ValueError, TypeError, KeyError, OSError, sqlite3.Error):
                    self._json(409, {"error": "VISIBLE_KNOWLEDGE_UNAVAILABLE"})
            elif path == "/api/agent-evaluation":
                try:
                    from .evaluation_delivery import read_evaluation

                    self._json(200, read_evaluation())
                except (ValueError, TypeError, KeyError, OSError):
                    self._json(409, {"error": "FROZEN_EVALUATION_UNAVAILABLE"})
            elif path == "/api/tasks" or path.startswith("/api/tasks/"):
                try:
                    parts = path.split("/")
                    if path == "/api/tasks":
                        result = tasks.list()
                    elif len(parts) == 4:
                        result = tasks.get(parts[3])
                    elif len(parts) == 5 and parts[4] == "view":
                        result = self._task_view(tasks, parts[3])
                    elif len(parts) == 5 and parts[4] == "events":
                        result = self._task_event_page(tasks, parts[3])
                    elif len(parts) == 5 and parts[4] == "report":
                        task = tasks.get(parts[3])
                        if task["report"] is None:
                            raise PlatformError("TASK_REPORT_NOT_READY")
                        result = {
                            "schema_version": "qf-task-report-v1",
                            "task_id": task["id"],
                            "mode": "OFFLINE_DEMO",
                            "report": task["report"],
                        }
                    else:
                        self._json(404, {"error": "ROUTE_NOT_FOUND"})
                        return
                    self._json(200, result)
                except (ValueError, TypeError, KeyError, OSError, sqlite3.Error):
                    self._json(409, {"error": "TASK_OR_REPORT_UNAVAILABLE"})
            elif path == "/api/snapshot":
                try:
                    self._json(200, service.snapshot())
                except (ValueError, sqlite3.DatabaseError):
                    self._json(409, {"error": "LEDGER_UNAVAILABLE"})
            elif path == "/api/events":
                self._stream()
            elif path == "/api/room":
                try:
                    self._json(200, room.snapshot())
                except (ValueError, KeyError, TypeError, OSError, sqlite3.Error):
                    self._json(409, {"error": "RESEARCH_ROOM_UNAVAILABLE"})
            elif path == "/api/room/evidence":
                try:
                    params = parse_qs(urlsplit(self.path).query, strict_parsing=True)
                    if set(params) != {"run_id"} or len(params["run_id"]) != 1:
                        raise ValueError
                    self._json(200, room.evidence(params["run_id"][0]))
                except (ValueError, OSError, sqlite3.Error):
                    self._json(409, {"error": "RESEARCH_EVIDENCE_UNAVAILABLE"})
            elif path == "/api/canary":
                from .canary import plan

                self._json(200, plan({}))
            elif path in {"/api/brokers", "/api/history"}:
                try:
                    if path == "/api/brokers":
                        result = brokers.snapshot()
                    else:
                        params = parse_qs(
                            urlsplit(self.path).query, strict_parsing=True
                        )
                        if set(params) - {"domain", "after", "limit"} or any(
                            len(v) != 1 for v in params.values()
                        ):
                            raise ValueError
                        domain = params.get("domain", ["simulation"])[0]
                        after = int(params.get("after", ["0"])[0])
                        limit = int(params.get("limit", ["50"])[0])
                        if after < 0 or not 1 <= limit <= 100:
                            raise ValueError
                        ledgers = {
                            "simulation": service.ledger,
                            "broker": brokers.ledger,
                        }
                        rows = ledgers[domain].read()
                        events = [v for v in rows if v["seq"] > after][:limit]
                        result = {
                            "domain": domain,
                            "events": events,
                            "next_cursor": events[-1]["seq"] if events else after,
                            "has_more": bool(events and events[-1]["seq"] < len(rows)),
                        }
                    self._json(200, result)
                except (ValueError, KeyError, TypeError, OSError, sqlite3.Error):
                    self._json(409, {"error": "OPERATIONS_UNAVAILABLE"})
            elif path == "/api/history-page":
                try:
                    params = parse_qs(urlsplit(self.path).query, strict_parsing=True)
                    if set(params) - {
                        "domain",
                        "after",
                        "limit",
                        "anchor",
                        "root_id",
                    } or any(len(values) != 1 for values in params.values()):
                        raise ValueError("PAGE_QUERY_INVALID")
                    domain = params.get("domain", ["simulation"])[0]
                    ledger = {"simulation": service.ledger, "broker": brokers.ledger}[
                        domain
                    ]
                    after = int(params.get("after", ["0"])[0])
                    root_id = hashlib.sha256(str(ledger.root).encode()).hexdigest()[:24]
                    if (after and params.get("root_id") != [root_id]) or (
                        "root_id" in params and params["root_id"] != [root_id]
                    ):
                        raise ValueError("PAGE_ROOT_CONFLICT")
                    result = ledger.page(
                        after=after,
                        limit=int(params.get("limit", ["50"])[0]),
                        anchor=params.get("anchor", [None])[0],
                    )
                    self._json(
                        200, {**result, "domain": domain, "live_authority": "PAUSED"}
                    )
                except (ValueError, KeyError, TypeError, OSError, sqlite3.Error):
                    self._json(409, {"error": "VERIFIED_SEGMENT_UNAVAILABLE"})
            elif path == "/api/bindings":
                try:
                    self._json(
                        200,
                        {
                            "schema_version": "qf-bindings-v1",
                            "profiles": bindings.list(),
                            "usage": bindings.model_usage(),
                            "roles": bindings.roles(),
                            "observed_at": datetime.now(UTC).isoformat(),
                            "live_authority": "PAUSED",
                            "operator_enabled": False,
                        },
                    )
                except (ValueError, OSError, sqlite3.Error):
                    self._json(409, {"error": "BINDING_STORE_UNAVAILABLE"})
            else:
                self._json(404, {"error": "ROUTE_NOT_FOUND"})

        def _stream(self) -> None:
            if not streams.acquire(blocking=False):
                self._json(429, {"error": "STREAM_CAPACITY"})
                return
            try:
                self.send_response(200)
                self._headers()
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                while True:
                    snapshot = service.snapshot()
                    frame = (
                        f"id: {snapshot['version']}\nevent: state\n"
                        f"data: {canonical(snapshot)}\n\n"
                    )
                    self.wfile.write(frame.encode())
                    self.wfile.flush()
                    time.sleep(1)
            except (OSError, ValueError, sqlite3.Error):
                return
            finally:
                streams.release()

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            # Consume bounded bodies before denial; unread bytes cause resets.
            limit = (
                MAX_REQUEST_BYTES
                if charts is not None and path == "/api/chart-analyses"
                else 65536
            )
            raw = self.rfile.read(length) if 0 < length <= limit else b""
            if chart_only and not path.startswith("/api/chart-"):
                self._json(403, {"error": "SCREENSHOT_PRODUCT_ONLY"})
                return
            if charts is not None and path.startswith("/api/chart-"):
                required_mode = (
                    "CONFIGURATION" if path == "/api/chart-settings" else "RESEARCH"
                )
                if (
                    not self._allowed()
                    or self.headers.get("X-QF-Mode") != required_mode
                ):
                    self._json(403, {"error": "EXPLICIT_LOCAL_MODE_REQUIRED"})
                    return
                try:
                    if (
                        not 0 < length <= limit
                        or self.headers.get("Transfer-Encoding") is not None
                        or self.headers.get("Content-Type") != "application/json"
                        or len(raw) != length
                    ):
                        raise PlatformError("BOUNDED_JSON_REQUIRED")
                    payload = json.loads(raw)
                    if not isinstance(payload, dict):
                        raise PlatformError("OBJECT_REQUIRED")
                    if path == "/api/chart-settings":
                        result = charts.configure(payload)
                    elif path == "/api/chart-analyses":
                        if payload.get("channel", "WEB") != "WEB":
                            raise PlatformError("CHART_CHANNEL_NOT_CONFIGURED")
                        result = charts.create(
                            payload, self.headers.get("Idempotency-Key", "")
                        )
                    elif (
                        len(path.split("/")) == 5
                        and path.split("/")[2] == "chart-analyses"
                        and path.endswith("/review")
                        and payload == {"action": "review"}
                    ):
                        from .chart_review import review

                        result = review(
                            charts.get(path.split("/")[3]), charts.market_data
                        )
                    elif (
                        len(path.split("/")) == 5
                        and path.endswith("/start")
                        and set(payload) == {"action"}
                        and payload["action"] == "start"
                    ):
                        result = charts.start(path.split("/")[3])
                    else:
                        self._json(404, {"error": "ROUTE_NOT_FOUND"})
                        return
                    self._json(200, result)
                except PlatformError as error:
                    self._json(chart_refusal_status(error), {"error": str(error)})
                except (ValueError, KeyError, TypeError, OSError, sqlite3.Error):
                    self._json(409, {"error": "CHART_INPUT_OR_STORE_UNAVAILABLE"})
                return
            binding_request = path.startswith("/api/bindings/")
            room_request = path.startswith("/api/room/")
            task_request = path == "/api/tasks" or path.startswith("/api/tasks/")
            agent_request = path == "/api/agent-tasks" or path.startswith(
                "/api/agent-tasks/"
            )
            operational_request = path.startswith(("/api/brokers/", "/api/canary/"))
            required_mode = (
                "CONFIGURATION"
                if binding_request
                else "RESEARCH"
                if room_request or task_request or agent_request
                else "OPERATIONS"
                if operational_request
                else "SIMULATION"
            )
            if not self._allowed() or self.headers.get("X-QF-Mode") != required_mode:
                self._json(
                    403,
                    {"error": "EXPLICIT_LOCAL_MODE_REQUIRED", "mode": required_mode},
                )
                return
            operations = {
                "/api/transfers": "transfer",
                "/api/control": "pause",
                "/api/research/cycle": "cycle",
                "/api/autonomy": "autonomy",
            }
            binding_operations = {
                "/api/bindings/save",
                "/api/bindings/delete",
                "/api/bindings/test",
                "/api/bindings/discover",
                "/api/bindings/assign",
            }
            room_operations = {
                "/api/room/start",
                "/api/room/control",
                "/api/room/datasets/demo",
                "/api/room/select",
            }
            operational_operations = {
                "/api/brokers/kill",
                "/api/brokers/refresh",
                "/api/brokers/quote",
                "/api/brokers/query",
                "/api/brokers/cancel",
                "/api/canary/plan",
            }
            task_route = path == "/api/tasks" or (
                len(path.split("/")) == 5 and path.endswith("/control") and task_request
            )
            agent_route = path == "/api/agent-tasks" or (
                len(path.split("/")) == 5
                and path.endswith("/control")
                and agent_request
            )
            if (
                path not in operations
                and path not in binding_operations
                and path not in room_operations
                and path not in operational_operations
                and not task_route
                and not agent_route
            ):
                self._json(404, {"error": "ROUTE_NOT_FOUND"})
                return
            if offline_demo and not task_route:
                self._json(403, {"error": "OFFLINE_DEMO_TASKS_ONLY"})
                return
            try:
                if (
                    not 0 < length <= 65536
                    or self.headers.get("Content-Type") != "application/json"
                    or self.headers.get("Transfer-Encoding") is not None
                ):
                    raise PlatformError("BOUNDED_JSON_REQUIRED")
                if len(raw) != length:
                    raise PlatformError("INCOMPLETE_REQUEST")
                payload = json.loads(raw)
                if not isinstance(payload, dict):
                    raise PlatformError("OBJECT_REQUIRED")
                if agent_request:
                    if path == "/api/agent-tasks":
                        result = agent_tasks.create(
                            payload, self.headers.get("Idempotency-Key", "")
                        )
                    else:
                        if set(payload) != {"action"}:
                            raise PlatformError("TASK_CONTROL_SCHEMA_INVALID")
                        result = agent_tasks.control(
                            path.split("/")[3], payload["action"]
                        )
                elif task_request:
                    if path == "/api/tasks":
                        result = tasks.create(
                            payload, self.headers.get("Idempotency-Key", "")
                        )
                    else:
                        if set(payload) != {"action"}:
                            raise PlatformError("TASK_CONTROL_SCHEMA_INVALID")
                        result = tasks.control(path.split("/")[3], payload["action"])
                elif room_request:
                    result = (
                        room.select(payload["program_id"])
                        if path.endswith("select")
                        else room.acquire_demo()
                        if path.endswith("datasets/demo")
                        else (
                            room.start(payload)
                            if path.endswith("start")
                            else room.control(payload["action"])
                        )
                    )
                elif operational_request:
                    action = path.rsplit("/", 1)[-1]
                    if path.startswith("/api/canary/"):
                        from .canary import plan

                        result = plan(payload)
                    elif path.startswith("/api/brokers/"):
                        result = brokers.operate(action, payload)
                elif binding_request:
                    if path == "/api/bindings/save":
                        if not isinstance(
                            payload.get("metadata"), dict
                        ) or not isinstance(payload.get("secrets"), dict):
                            raise PlatformError("BINDING_CONFIGURATION_INVALID")
                        bindings.save(
                            payload["metadata"],
                            payload["secrets"],
                            payload.get("profile_id"),
                        )
                    elif path == "/api/bindings/delete":
                        bindings.delete(payload["profile_id"])
                    elif path in {"/api/bindings/test", "/api/bindings/discover"}:
                        bindings.test(
                            payload["profile_id"], discover=path.endswith("discover")
                        )
                    else:
                        bindings.assign(payload["role"], payload["routes"])
                    result = {
                        "schema_version": "qf-bindings-v1",
                        "profiles": bindings.list(),
                        "usage": bindings.model_usage(),
                        "roles": bindings.roles(),
                        "observed_at": datetime.now(UTC).isoformat(),
                        "live_authority": "PAUSED",
                        "operator_enabled": False,
                    }
                else:
                    result = service.operate(
                        operations[path],
                        payload,
                        self.headers.get("Idempotency-Key", ""),
                    )
                self._json(200, result)
            except PlatformError as exc:
                self._json(409, {"error": str(exc), "mode": required_mode})
            except (ValueError, TypeError, KeyError, OSError, sqlite3.Error):
                self._json(
                    409, {"error": "INPUT_OR_STORE_UNAVAILABLE", "mode": required_mode}
                )

    server = PlatformServer((host, port), Handler)
    server.supervisor = None
    server.task_readers = (tasks, agent_tasks)
    server.scheduler_stop = threading.Event()

    def schedule() -> None:
        while not server.scheduler_stop.wait(1):
            if not embedded_scheduler:
                continue
            for tick in (service.autonomous_tick, tasks.tick, agent_tasks.tick):
                try:
                    tick()
                except (ValueError, OSError, sqlite3.Error):
                    # A broken domain does not fabricate success or block peers.
                    continue

    server.scheduler = threading.Thread(target=schedule, daemon=True)
    server.scheduler.start()
    return server


def main() -> None:
    parser = argparse.ArgumentParser(
        description="QuantForge simulation API; no live funds"
    )
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--port", type=int, default=4188)
    parser.add_argument("--host", choices=("127.0.0.1", "0.0.0.0"), default="127.0.0.1")
    parser.add_argument("--offline-demo", action="store_true")
    parser.add_argument("--chart-assistant", action="store_true")
    parser.add_argument("--chart-only", action="store_true")
    args = parser.parse_args()
    if args.chart_only:
        os.environ["QF_CHART_ONLY"] = "1"
    if not 1024 <= args.port <= 65535:
        parser.error("port outside range")
    if args.chart_only:
        import uvicorn

        from .chart_api import create_app

        uvicorn.run(
            create_app(args.root, port=args.port, manage_worker=True),
            host=args.host,
            port=args.port,
            access_log=False,
        )
        return
    server = make_server(
        args.root,
        args.port,
        embedded_scheduler=False,
        host=args.host,
        offline_demo=args.offline_demo,
        chart_assistant=args.chart_assistant,
        chart_only=args.chart_only,
    )

    def stop(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    print(
        f"SIMULATION API http://{args.host}:{args.port}; live transport LOCKED",
        flush=True,
    )
    try:
        server.supervisor = (
            WorkerSupervisor(args.root, domains=("assistant-tasks",))
            if args.chart_only
            else WorkerSupervisor(args.root)
        )
        server.supervisor.start()
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
