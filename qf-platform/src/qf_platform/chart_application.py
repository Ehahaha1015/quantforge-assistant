"""Screenshot use cases shared by HTTP, offline demonstrations and diagnostics."""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
from typing import Any

from .chart_analysis import ChartAnalysisService
from .chart_contracts import EventsResponse
from .chart_reports import ChartRequest
from .chart_runtime import ChartRuntime
from .storage import PlatformError


class ChartApplication:
    def __init__(self, root: Path, *, runtime: ChartRuntime | None = None) -> None:
        self.charts = ChartAnalysisService(root, runtime=runtime)

    def submit(self, request: ChartRequest, request_id: str) -> dict[str, Any]:
        if request.channel != "WEB":
            raise PlatformError("CHART_CHANNEL_NOT_CONFIGURED")
        return self.charts.create(request.model_dump(), request_id)

    def events(self, job_id: str) -> EventsResponse:
        self.charts.get(job_id)
        with closing(self.charts.connect()) as db:
            db.execute("BEGIN")
            rows = self.charts.repository.events(db)
        items = []
        for seq, row in enumerate(rows, 1):
            if row.get("job_id") == job_id:
                snapshot = row.get("snapshot") or {}
                items.append(
                    {
                        "seq": seq,
                        "at": row["at"],
                        "kind": row["kind"],
                        "status": snapshot.get("status"),
                        "error": snapshot.get("error"),
                    }
                )
        return EventsResponse(job_id=job_id, items=items)
