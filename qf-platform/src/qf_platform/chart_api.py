"""FastAPI screenshot product; legacy research routes are deliberately absent."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response

from .chart_application import ChartApplication
from .chart_contracts import (
    AnalysisListResponse,
    AnalysisResponse,
    ConfigurationCommand,
    EventsResponse,
    ReviewCommand,
    SettingsResponse,
    StartCommand,
)
from .chart_forward import DailyForward
from .chart_http import HttpObservations, LocalChartMiddleware
from .chart_reports import ChartRequest, markdown
from .chart_runtime import ChartRuntime
from .storage import PlatformError
from .supervisor import WorkerSupervisor


def create_app(
    root: Path,
    *,
    port: int = 4188,
    manage_worker: bool = False,
    runtime: ChartRuntime | None = None,
    demo: bool = False,
) -> FastAPI:
    if demo:
        from .chart_demo import OfflineChartRuntime, check_demo_root

        check_demo_root(root)
        if not isinstance(runtime, OfflineChartRuntime):
            raise PlatformError("DEMO_OFFLINE_RUNTIME_REQUIRED")
    application = ChartApplication(root, runtime=runtime)
    charts, observations = application.charts, HttpObservations()
    forward = DailyForward(root)
    supervisor: WorkerSupervisor | None = None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal supervisor
        if manage_worker:
            supervisor = WorkerSupervisor(root, domains=("assistant-tasks",), demo=demo)
            supervisor.start()
        try:
            yield
        finally:
            if supervisor:
                supervisor.close()

    app = FastAPI(
        title="QuantForge Screenshot Assistant",
        version="1.0.0",
        openapi_url="/api/openapi.json",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.application = application
    app.add_middleware(LocalChartMiddleware, port=port, observations=observations)

    @app.exception_handler(PlatformError)
    async def refusal(request: Request, error: PlatformError) -> JSONResponse:
        status = 404 if str(error) in {"CHART_ID_INVALID", "CHART_NOT_FOUND"} else 409
        return JSONResponse({"error": str(error)}, status_code=status)

    @app.exception_handler(RequestValidationError)
    async def validation(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        code = (
            "CHART_REQUEST_INVALID"
            if request.url.path == "/api/chart-analyses"
            else "CHART_CONFIGURATION_INVALID"
            if request.url.path == "/api/chart-settings"
            else "CHART_ACTION_INVALID"
        )
        return JSONResponse({"error": code}, status_code=409)

    async def unavailable(request: Request, error: Exception) -> JSONResponse:
        return JSONResponse({"error": "CHART_OR_REPORT_UNAVAILABLE"}, status_code=409)

    for exception in (ValueError, KeyError, OSError, sqlite3.Error):
        app.add_exception_handler(exception, unavailable)

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {
            "schema_version": "qf-health-v1",
            "status": "OK",
            "service": "quantforge-chart-assistant",
            "observed_at": datetime.now(UTC).isoformat(),
            "mode": "SIMULATION",
            "real_transport": "LOCKED",
            "task_mode": "OFFLINE_DEMO" if demo else "CHART_ONLY",
            "agent_task_mode": "DISABLED",
            "deployment_profile": "SCREENSHOT_ASSISTANT",
            "worker_mode": "EXTERNAL_PROCESSES",
            "workers": supervisor.snapshot() if supervisor else {},
            "runtime_id": hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:24],
            "http_framework": "FastAPI",
            "demo": demo,
        }

    @app.get("/api/docs", response_class=HTMLResponse)
    def docs() -> str:
        return (
            '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            "<title>QuantForge API</title><h1>QuantForge Screenshot API</h1>"
            '<p><a href="./openapi.json">OpenAPI 3.1 JSON</a></p>'
            "<p>POST需显式X-QF-Mode；截图上传需Idempotency-Key。仅本机访问。</p></html>"
        )

    @app.get(
        "/api/chart-settings",
        response_model=SettingsResponse,
        response_model_exclude_unset=True,
    )
    def settings() -> dict[str, Any]:
        return charts.status()

    @app.post(
        "/api/chart-settings",
        response_model=SettingsResponse,
        response_model_exclude_unset=True,
    )
    def configure(command: ConfigurationCommand) -> dict[str, Any]:
        if demo:
            raise PlatformError("DEMO_CONFIGURATION_LOCKED")
        return charts.configure(
            {
                "api_key": command.api_key.get_secret_value(),
                "max_cost_cny": command.max_cost_cny,
            }
        )

    @app.get("/api/chart-analyses", response_model=AnalysisListResponse)
    def listing() -> dict[str, Any]:
        return charts.list()

    @app.post(
        "/api/chart-analyses",
        response_model=AnalysisResponse,
        response_model_exclude_unset=True,
    )
    def submit(
        command: ChartRequest, idempotency_key: str = Header(default="")
    ) -> dict[str, Any]:
        return application.submit(command, idempotency_key)

    @app.get(
        "/api/chart-analyses/{job_id}",
        response_model=AnalysisResponse,
        response_model_exclude_unset=True,
    )
    def get(job_id: str) -> dict[str, Any]:
        return charts.get(job_id)

    @app.post(
        "/api/chart-analyses/{job_id}/start",
        response_model=AnalysisResponse,
        response_model_exclude_unset=True,
    )
    def start(job_id: str, command: StartCommand) -> dict[str, Any]:
        return charts.start(job_id)

    @app.post("/api/chart-analyses/{job_id}/review")
    def review(job_id: str, command: ReviewCommand) -> dict[str, Any]:
        if demo:
            raise PlatformError("DEMO_EXTERNAL_DATA_DISABLED")
        from .chart_review import review as review_report

        return review_report(charts.get(job_id), charts.market_data)

    @app.get("/api/chart-analyses/{job_id}/observation")
    def observation(job_id: str) -> dict[str, Any]:
        if demo:
            raise PlatformError("DEMO_EXTERNAL_DATA_DISABLED")
        from .chart_followup import observe

        return observe(charts.get(job_id), charts.market_data)

    @app.get("/api/chart-analyses/{job_id}/events", response_model=EventsResponse)
    def events(job_id: str) -> EventsResponse:
        return application.events(job_id)

    @app.get("/api/chart-analyses/{job_id}/image")
    def image(job_id: str) -> Response:
        raw, mime = charts.image(job_id)
        return Response(raw, media_type=mime)

    @app.get("/api/chart-analyses/{job_id}/report.md")
    def report(job_id: str) -> Response:
        value = charts.get(job_id)
        if value["report"] is None:
            raise PlatformError("CHART_REPORT_NOT_READY")
        return Response(
            markdown(value),
            media_type="text/markdown",
            headers={"Content-Disposition": f'attachment; filename="{job_id}.md"'},
        )

    @app.get("/api/chart-forward")
    def forward_status() -> dict[str, Any]:
        return forward.status()

    @app.get("/api/chart-http-observations")
    def http_observations() -> dict[str, Any]:
        return observations.snapshot()

    return app
