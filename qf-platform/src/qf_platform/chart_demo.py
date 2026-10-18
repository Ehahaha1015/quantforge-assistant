"""Explicit, isolated offline adapters and synthetic charts; never provider accuracy."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import math
import os
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import polars as pl
from PIL import Image, ImageDraw
from quantforge.data.bars import logical_content_hash
from quantforge.product.context import source_identity
from quantforge.strategies.chart_quality import is_session

from . import chart_algorithms, chart_provider
from .chart_analysis import ChartAnalysisService
from .chart_reports import ChartReport
from .storage import PlatformError, canonical

MARKER = {"mode": "OFFLINE_SHOWCASE", "version": 1, "real_model_calls": 0}
SCENARIOS = ("success", "insufficient", "invalid", "unknown")
FIXTURE_KEY = "offline-showcase-not-a-provider-credential"


def check_demo_root(root: Path, *, initialize: bool = False) -> Path:
    absolute = root.absolute()
    for path in (absolute, *absolute.parents):
        if path.is_symlink() or path.is_junction():
            raise PlatformError("DEMO_REDIRECTED_ROOT")
    marker = absolute / "DEMO_ONLY.json"
    if not marker.exists():
        if not initialize or (absolute.exists() and any(absolute.iterdir())):
            raise PlatformError("DEMO_REQUIRES_EMPTY_DEDICATED_ROOT")
        absolute.mkdir(parents=True, exist_ok=True)
        with marker.open("x", encoding="utf-8") as stream:
            stream.write(canonical(MARKER))
    if marker.is_symlink() or json.loads(marker.read_bytes()) != MARKER:
        raise PlatformError("DEMO_MARKER_INVALID")
    return absolute


def sample_image(scenario: str) -> bytes:
    image = Image.new("RGB", (800, 420), "#101c2e")
    draw = ImageDraw.Draw(image)
    draw.text((24, 20), "SYNTHETIC OFFLINE DEMO - 600519 - 1D RAW", fill="white")
    draw.text((24, 44), f"2026-09-01 / 2026-09-30 - {scenario}", fill="white")
    for i in range(60):
        x, y = 40 + i * 12, 290 - int(i * 2 + 12 * math.sin(i / 3))
        draw.line((x, y - 17, x, y + 18), fill="#b6c9db", width=1)
        draw.rectangle((x - 3, y - 7, x + 3, y + 7), fill="#45c7a8")
        draw.rectangle((x - 3, 380 - i % 8 * 4, x + 3, 395), fill="#45c7a8")
    stream = io.BytesIO()
    image.save(stream, "PNG")
    return stream.getvalue()


def sample_request(scenario: str = "success") -> dict[str, Any]:
    return {
        "symbol": "600519",
        "display_name": "离线合成示例",
        "period_start": "2026-09-01",
        "period_end": "2026-09-30",
        "adjustment": "RAW",
        "include_context": False,
        "question": "demo:" + scenario,
        "image_base64": base64.b64encode(sample_image(scenario)).decode(),
    }


def sample_report(
    metadata: dict[str, Any], *, insufficient: bool = False
) -> ChartReport:
    return ChartReport.model_validate(
        {
            "image_quality": "UNREADABLE" if insufficient else "READABLE",
            "recognized_symbol": None if insufficient else metadata["symbol"],
            "recognized_period": None
            if insufficient
            else metadata["period_start"] + " 至 " + metadata["period_end"],
            "recognized_timeframe": None
            if insufficient
            else metadata.get("timeframe", "1d"),
            "recognized_adjustment": "UNKNOWN" if insufficient else "RAW",
            "summary": "离线合成样例，用于演示任务与校验流程；未调用真实模型。",
            "observed_trend": "UNCLEAR",
            "outlook": "UNCLEAR",
            "observations": []
            if insufficient
            else [
                {
                    "observation": "合成图示例包含K线及成交量绘图区。",
                    "image_region": "图像主体",
                }
            ],
            "support_zones": [],
            "resistance_zones": [],
            "volume_comment": "合成数据不能证明真实交易量。",
            "scenarios": []
            if insufficient
            else [
                {
                    "direction": d,
                    "trigger": "满足对应方向的完整证据。",
                    "interpretation": "仅为软件演示的条件性情景。",
                    "invalidation": "资料不完整时不成立。",
                }
                for d in ("BULLISH", "RANGE", "BEARISH")
            ],
            "uncertainties": ["明确的离线软件夹具，不是真实模型效果或市场证据。"],
        }
    )


class OfflineChartRuntime:
    def __init__(self, root: Path, *, crash_at: str | None = None) -> None:
        self.root = check_demo_root(root)
        self.crash_at = crash_at

    def checkpoint(self, stage: str) -> None:
        if stage == self.crash_at:
            os._exit(73)

    def algorithm(
        self,
        metadata: dict[str, Any],
        root: Path,
        *,
        initial_levels: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if metadata.get("timeframe", "1d") != "1d":
            raise ValueError("Offline samples support daily charts only")
        end = date.fromisoformat(metadata["period_end"])
        days = [end - timedelta(days=i) for i in reversed(range(250))]
        days = [d for d in days if is_session(d, end) is True]
        if len(days) < 80:
            raise ValueError("Synthetic sample outside supported calendar")
        closes = [100 + i * 0.07 + math.sin(i / 6) for i in range(len(days))]
        frame = pl.DataFrame(
            {
                "symbol": [metadata["symbol"]] * len(days),
                "timestamp": [datetime.combine(d, time(7), UTC) for d in days],
                "open": closes,
                "high": [x + 1 for x in closes],
                "low": [x - 1 for x in closes],
                "close": closes,
                "volume": [10000.0] * len(days),
            }
        )
        result = chart_algorithms.evaluate(
            frame,
            date.fromisoformat(metadata["period_start"]),
            metadata,
            initial_levels,
        )
        raw = canonical(
            {
                "fixture": "SYNTHETIC_NOT_MARKET_DATA",
                "bars": frame.with_columns(
                    pl.col("timestamp").cast(pl.String)
                ).to_dicts(),
            }
        ).encode()
        sha = hashlib.sha256(raw).hexdigest()
        path = root / (sha + ".json")
        if path.is_symlink():
            raise ValueError("Redirected synthetic artifact")
        if not path.exists():
            path.write_bytes(raw)
        elif path.read_bytes() != raw:
            raise ValueError("Synthetic artifact differs")
        return {
            "schema_version": chart_algorithms.VERSION,
            "status": "AVAILABLE",
            "reason": None,
            "core_source_sha256": source_identity(),
            **result,
            "timeframe": "1d",
            "source": {
                "provider": "OFFLINE_SYNTHETIC",
                "symbol": metadata["symbol"],
                "timeframe": "1d",
                "adjustment": "RAW_UNADJUSTED",
                "url": "demo://synthetic-bars",
                "raw_sha256": sha,
                "bars_sha256": logical_content_hash(frame),
                "cutoff": end.isoformat(),
                "artifact": path.name,
                "bars": len(days),
                "first": days[0].isoformat(),
                "last": days[-1].isoformat(),
                "last_timestamp": frame["timestamp"][-1].isoformat(),
                "retrieved_at": datetime.now(UTC).isoformat(),
            },
            "cross_timeframe": {"status": "UNAVAILABLE", "items": []},
            "limitations": ["合成行情，只演示计算链路。"],
            "forecast_validated": False,
            "screenshot_identity_verified": False,
        }

    def analyze(
        self,
        key: str,
        data: bytes,
        mime: str,
        metadata: dict[str, Any],
        algorithm: dict[str, Any] | None,
    ) -> tuple[ChartReport, dict[str, Any]]:
        # This durable counter makes process-crash checks independent of memory.
        with (self.root / "offline-calls.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(
                canonical(
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "image_sha256": hashlib.sha256(data).hexdigest(),
                        "real_model_call": False,
                    }
                )
                + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        self.checkpoint("DURING_SEND")
        scenario = metadata.get("question", "").removeprefix("demo:")
        if scenario == "unknown":
            raise chart_provider.UnknownChartCall("CHART_PROVIDER_EFFECT_UNKNOWN")
        if scenario == "invalid":
            return chart_provider.parse_response(
                b'{"choices": [{"finish_reason":"stop",'
                b'"message":{"content":"bad json"}}]}'
            )
        return sample_report(metadata, insufficient=scenario == "insufficient"), {
            "model": "OFFLINE_FIXTURE_NOT_A_MODEL",
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }


def prepare_demo(root: Path) -> list[str]:
    root = check_demo_root(root, initialize=True)
    app = ChartAnalysisService(root, runtime=OfflineChartRuntime(root))
    if not app.status()["configured"]:
        app.configure({"api_key": FIXTURE_KEY, "max_cost_cny": "10.00"})
    with app.connect() as db:
        secret = app._configuration(db)["secret"]
    if app._cipher().decrypt(secret.encode()).decode() != FIXTURE_KEY:
        raise PlatformError("DEMO_REAL_CONFIGURATION_REFUSED")
    return [app.create(sample_request(s), "offline-demo-" + s)["id"] for s in SCENARIOS]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--port", type=int, default=4192)
    parser.add_argument("--host", choices=("127.0.0.1", "0.0.0.0"), default="127.0.0.1")
    args = parser.parse_args()
    prepare_demo(args.root)
    import uvicorn

    from .chart_api import create_app
    from .chart_demo import OfflineChartRuntime as CanonicalOfflineRuntime

    uvicorn.run(
        create_app(
            args.root,
            port=args.port,
            manage_worker=True,
            runtime=CanonicalOfflineRuntime(args.root),
            demo=True,
        ),
        host=args.host,
        port=args.port,
        access_log=False,
    )


if __name__ == "__main__":
    main()
