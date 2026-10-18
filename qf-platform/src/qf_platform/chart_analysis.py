"""Durable screenshot jobs and a separate, append-only model-call budget domain."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from cryptography.fernet import Fernet, InvalidToken
from pydantic import ValidationError
from quantforge.product.storage import local_root, writer_lock

from . import chart_algorithms, chart_provider
from .chart_reports import (
    STRATEGY_VERSION,
    ChartReport,
    ChartRequest,
    assess_alignment,
    instructions,
    validate_image,
)
from .chart_repository import ChartRepository
from .chart_runtime import ChartRuntime, LiveChartRuntime
from .chart_timeframes import cutoff_at
from .storage import PlatformError, canonical, digest

MAX_JOBS = 100
MAX_CALLS = 100
# Conservative DEVELOPMENT estimates: official peak USD0.30/1.20 per million,
# multiplied by a declared CNY10/USD reserve factor. Not a provider bill/FX quote.
INPUT_CNY = Decimal("3.00")
OUTPUT_CNY = Decimal("12.00")
_JOB = re.compile(r"chart-[a-f0-9]{32}\Z")


def stamp() -> str:
    return datetime.now(UTC).isoformat()


class ChartAnalysisService:
    def __init__(self, root: Path, *, runtime: ChartRuntime | None = None) -> None:
        directory = local_root(root) / "chart-assistant"
        directory.mkdir(exist_ok=True)
        self.root = local_root(directory)
        self.images = self.root / "images"
        self.images.mkdir(exist_ok=True)
        local_root(self.images)
        self.market_data = self.root / "market-data"
        self.market_data.mkdir(exist_ok=True)
        local_root(self.market_data)
        self.repository = ChartRepository(self.root)
        self.path = self.repository.path
        self.runtime = runtime or LiveChartRuntime()

    def connect(self) -> sqlite3.Connection:
        return self.repository.connect()

    _events = staticmethod(ChartRepository.events)
    _append = staticmethod(ChartRepository.append)

    def _cipher(self) -> Fernet:
        path = self.root / "local-key.bin"
        if path.is_symlink():
            raise PlatformError("CHART_SECRET_STORE_REDIRECTED")
        if not path.exists():
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(Fernet.generate_key())
            except FileExistsError:
                pass
        return Fernet(path.read_bytes())

    def _configuration(self, db: sqlite3.Connection) -> dict[str, Any]:
        row = db.execute("SELECT payload FROM settings WHERE id=1").fetchone()
        return json.loads(row[0]) if row else {"secret": None, "max_cost_cny": "0.00"}

    def status(self) -> dict[str, Any]:
        from .qq_status import connected

        with closing(self.connect()) as db:
            db.execute("BEGIN")
            settings = self._configuration(db)
            events = self._events(db)
            calls = [row for row in events if row["kind"] == "MODEL_CALL_RESERVED"]
            reserved = sum((Decimal(row["reserved_cny"]) for row in calls), Decimal(0))
            cap = Decimal(settings["max_cost_cny"])
            return {
                "schema_version": "chart-provider-status-v1",
                "provider": "DeepSeek",
                "model": chart_provider.MODEL,
                "configured": bool(settings["secret"]),
                "ready": bool(settings["secret"])
                and cap > reserved
                and len(calls) < MAX_CALLS,
                "max_cost_cny": str(cap),
                "reserved_cny": str(reserved),
                "calls_used": len(calls),
                "calls_limit": MAX_CALLS,
                "currency": "CNY_CONSERVATIVE_RESERVE_ESTIMATE_NOT_PROVIDER_BILL",
                "qq_connected": connected(self.root.parent),
                "strategy_version": STRATEGY_VERSION,
            }

    def configure(self, payload: dict[str, Any]) -> dict[str, Any]:
        if set(payload) - {"api_key", "max_cost_cny"} or "max_cost_cny" not in payload:
            raise PlatformError("CHART_CONFIGURATION_INVALID")
        amount = payload["max_cost_cny"]
        if not isinstance(amount, str) or not re.fullmatch(
            r"(?:0|[1-9][0-9]{0,3})(?:\.[0-9]{1,2})?", amount
        ):
            raise PlatformError("CHART_BUDGET_INVALID")
        key = payload.get("api_key", "")
        if (
            not isinstance(key, str)
            or len(key) > 512
            or (key and (len(key) < 8 or any(char.isspace() for char in key)))
        ):
            raise PlatformError("CHART_API_KEY_INVALID")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM jobs WHERE json_extract(payload,'$.status')='RUNNING'"
            ).fetchone():
                raise PlatformError("CHART_CONFIGURATION_BUSY")
            settings = self._configuration(db)
            if Decimal(amount) < Decimal(settings["max_cost_cny"]):
                raise PlatformError("CHART_BUDGET_CANNOT_RESET")
            if key:
                settings["secret"] = self._cipher().encrypt(key.encode()).decode()
            settings.update(max_cost_cny=amount, updated_at=stamp())
            if not settings["secret"]:
                raise PlatformError("CHART_API_KEY_REQUIRED")
            db.execute(
                "INSERT INTO settings VALUES(1,?) ON CONFLICT(id) "
                "DO UPDATE SET payload=excluded.payload",
                (canonical(settings),),
            )
            self._append(
                db,
                "CONFIGURATION_AUTHORIZED",
                max_cost_cny=amount,
                model=chart_provider.MODEL,
                secret_changed=bool(key),
            )
        return self.status()

    @classmethod
    def _put(cls, db: sqlite3.Connection, value: dict[str, Any], kind: str) -> None:
        value["updated_at"] = stamp()
        cls._append(db, kind, job_id=value["id"], snapshot=value)
        db.execute(
            "UPDATE jobs SET payload=? WHERE id=?", (canonical(value), value["id"])
        )

    def _read(self, db: sqlite3.Connection, job_id: str) -> dict[str, Any]:
        if not _JOB.fullmatch(job_id):
            raise PlatformError("CHART_ID_INVALID")
        events = self._events(db)
        snapshot = next(
            (
                row["snapshot"]
                for row in reversed(events)
                if row.get("job_id") == job_id and "snapshot" in row
            ),
            None,
        )
        row = db.execute(
            "SELECT payload,request_id,signature FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        if row is None or snapshot is None:
            raise PlatformError("CHART_NOT_FOUND")
        value = json.loads(row[0])
        if (
            value != snapshot
            or value["request_id"] != row[1]
            or value["signature"] != row[2]
        ):
            raise PlatformError("CHART_PROJECTION_INTEGRITY_FAILED")
        return cast(dict[str, Any], value)

    def get(self, job_id: str) -> dict[str, Any]:
        with closing(self.connect()) as db:
            db.execute("BEGIN")
            value = self._read(db, job_id)
            self._image_bytes(value)
            algorithm = value.get("algorithm_analysis")
            if algorithm and algorithm["status"] == "AVAILABLE":
                sources = [algorithm["source"]] + [
                    x["source"]
                    for x in algorithm.get("cross_timeframe", {}).get("items", [])
                    if x.get("source") is not None
                ]
                for source in sources:
                    self._verify_market_source(source)
                for source in algorithm.get("research_sources", []):
                    self._verify_market_source(source)
            return value

    def _verify_market_source(self, source: dict[str, Any]) -> None:
        if not re.fullmatch(r"[a-f0-9]{64}", source["raw_sha256"]):
            raise PlatformError("CHART_MARKET_DATA_INTEGRITY_FAILED")
        artifact = source["raw_sha256"] + ".json"
        if source["artifact"] != artifact:
            raise PlatformError("CHART_MARKET_DATA_INTEGRITY_FAILED")
        path = local_root(self.market_data) / artifact
        if (
            path.is_symlink()
            or not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != source["raw_sha256"]
        ):
            raise PlatformError("CHART_MARKET_DATA_INTEGRITY_FAILED")

    def list(self) -> dict[str, Any]:
        with closing(self.connect()) as db:
            db.execute("BEGIN")
            ids = [
                row[0] for row in db.execute("SELECT id FROM jobs ORDER BY rowid DESC")
            ]
            keys = ("id", "status", "request", "created_at", "updated_at", "image")
            return {
                "schema_version": "chart-list-v1",
                "items": [
                    {key: value[key] for key in keys}
                    for value in [self._read(db, item) for item in ids]
                ],
            }

    def create(self, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", request_id):
            raise PlatformError("CHART_IDEMPOTENCY_KEY_REQUIRED")
        try:
            request = ChartRequest.model_validate(payload)
        except ValidationError as error:
            raise PlatformError("CHART_REQUEST_INVALID") from error
        image, metadata = validate_image(request.image_base64)
        values = request.model_dump(exclude={"image_base64"})
        signature = digest(
            {"request": values, "image": metadata, "strategy": STRATEGY_VERSION}
        )
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT id,signature FROM jobs WHERE request_id=?", (request_id,)
            ).fetchone()
            if prior:
                if prior[1] != signature:
                    raise PlatformError("CHART_IDEMPOTENCY_CONFLICT")
                return self._read(db, prior[0])
            if request.position_reference_id:
                self._position_reference(db, values)
            if db.execute("SELECT count(*) FROM jobs").fetchone()[0] >= MAX_JOBS:
                raise PlatformError("CHART_WORKSPACE_FULL")
            prior_unknown = db.execute(
                "SELECT id FROM jobs WHERE json_extract(payload,'$.image.sha256')=? "
                "AND json_extract(payload,'$.status')='UNKNOWN'",
                (metadata["sha256"],),
            ).fetchone()
            if prior_unknown:
                self._read(db, prior_unknown[0])
                raise PlatformError("CHART_PRIOR_CALL_REQUIRES_REVIEW")
            path = self.images / (metadata["sha256"] + ".image")
            if path.is_symlink():
                raise PlatformError("CHART_IMAGE_REDIRECTED")
            if path.exists():
                if path.read_bytes() != image:
                    raise PlatformError("CHART_IMAGE_INTEGRITY_FAILED")
            else:
                with path.open("xb") as stream:
                    stream.write(image)
            value = {
                "schema_version": "chart-analysis-v1",
                "id": "chart-" + uuid.uuid4().hex,
                "request_id": request_id,
                "signature": signature,
                "request": values,
                "image": metadata,
                "strategy_version": STRATEGY_VERSION,
                "created_at": stamp(),
                "status": "QUEUED"
                if self._configuration(db)["secret"]
                else "WAITING_FOR_MODEL",
                "report": None,
                "model_call": None,
                "algorithm_analysis": None,
                "image_alignment": None,
                "error": None,
                "live_authority": "NONE",
                "forecast_validated": False,
            }
            db.execute(
                "INSERT INTO jobs VALUES(?,?,?,?)",
                (value["id"], request_id, signature, "{}"),
            )
            self._put(db, value, "SCREENSHOT_RECEIVED")
            return value

    def _position_reference(
        self, db: sqlite3.Connection, request: dict[str, Any]
    ) -> dict[str, Any] | None:
        parent_id = request.get("position_reference_id")
        if not parent_id:
            return None
        parent = self._read(db, parent_id)
        meta = parent["request"]
        algorithm = parent.get("algorithm_analysis") or {}
        plan = algorithm.get("trade_plan") or {}
        try:
            same_symbol = chart_algorithms.symbol_code(
                meta["symbol"]
            ) == chart_algorithms.symbol_code(request["symbol"])
        except (ValueError, KeyError) as error:
            raise PlatformError("CHART_POSITION_REFERENCE_INVALID") from error
        if (
            parent["status"] != "SUCCEEDED"
            or not parent.get("report")
            or assess_alignment(meta, ChartReport.model_validate(parent["report"]))[
                "status"
            ]
            != "CONSISTENT"
            or not same_symbol
            or meta.get("timeframe", "1d") != request["timeframe"]
            or request["held_bars"] < meta.get("held_bars", 0)
            or meta.get("purchase_price") is None
            or Decimal(meta["purchase_price"]) != Decimal(request["purchase_price"])
            or not plan.get("levels")
            or plan.get("position_basis") != "USER_COST"
            or cutoff_at(meta) > cutoff_at(request)
        ):
            raise PlatformError("CHART_POSITION_REFERENCE_INVALID")
        if algorithm.get("source"):
            self._verify_market_source(algorithm["source"])
        return cast(dict[str, Any], plan["levels"])

    def image(self, job_id: str) -> tuple[bytes, str]:
        value = self.get(job_id)
        return self._image_bytes(value)

    def _image_bytes(self, value: dict[str, Any]) -> tuple[bytes, str]:
        local_root(self.images)
        path = self.images / (value["image"]["sha256"] + ".image")
        if path.is_symlink():
            raise PlatformError("CHART_IMAGE_REDIRECTED")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != value["image"]["sha256"]:
            raise PlatformError("CHART_IMAGE_INTEGRITY_FAILED")
        return data, value["image"]["mime"]

    def start(self, job_id: str) -> dict[str, Any]:
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            value = self._read(db, job_id)
            if value["status"] != "WAITING_FOR_MODEL":
                raise PlatformError("CHART_START_REQUIRES_WAITING")
            if not self._configuration(db)["secret"]:
                raise PlatformError("CHART_API_KEY_REQUIRED")
            value.update(status="QUEUED", error=None)
            self._put(db, value, "ANALYSIS_QUEUED")
            return value

    def tick(self) -> bool:
        try:
            with writer_lock(self.root):
                return self._step()
        except OSError:
            return False

    def _step(self) -> bool:
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            # The OS fence spans the real call. A RUNNING job observed after
            # acquiring it belongs to an interrupted process, not a live peer.
            interrupted = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM jobs "
                    "WHERE json_extract(payload,'$.status')='RUNNING'"
                )
            ]
            for job_id in interrupted:
                value = self._read(db, job_id)
                value.update(status="UNKNOWN", error="CHART_PROVIDER_EFFECT_UNKNOWN")
                self._put(db, value, "INTERRUPTED_CALL_NOT_REPLAYED")
            queued = db.execute(
                "SELECT id FROM jobs WHERE json_extract(payload,'$.status')='QUEUED' "
                "ORDER BY rowid LIMIT 1"
            ).fetchone()
            if not queued:
                return bool(interrupted)
            value = self._read(db, queued[0])
            settings = self._configuration(db)
            if not settings["secret"]:
                value.update(status="WAITING_FOR_MODEL", error="CHART_API_KEY_REQUIRED")
                self._put(db, value, "MODEL_NOT_CONFIGURED")
                return True
        # Public data retrieval is outside the SQLite transaction. The OS fence
        # still excludes competing workers; restarting this unpaid step is safe.
        if value.get("algorithm_analysis") is None:
            try:
                with closing(self.connect()) as db:
                    initial_levels = self._position_reference(db, value["request"])
            except (PlatformError, ValueError, OSError):
                value.update(status="FAILED", error="CHART_POSITION_REFERENCE_INVALID")
                with closing(self.connect()) as db, db:
                    db.execute("BEGIN IMMEDIATE")
                    self._read(db, value["id"])
                    self._put(db, value, "POSITION_REFERENCE_REFUSED_BEFORE_SEND")
                return True
            value["algorithm_analysis"] = self.runtime.algorithm(
                value["request"], self.market_data, initial_levels=initial_levels
            )
            with closing(self.connect()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                self._read(db, value["id"])
                self._put(db, value, "CORE_ALGORITHMS_RECORDED")
        self.runtime.checkpoint("BEFORE_RESERVATION")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            settings = self._configuration(db)
            # UTF-8 bytes bound text tokens, plus a 4096-token image reserve
            # (official current image cap is 1024); no base64-as-text token guess.
            prompt = {
                "instructions": instructions(),
                "schema": ChartReport.model_json_schema(),
                "metadata": value["request"],
                "deterministic_core_analysis": chart_provider.model_facts(
                    value["algorithm_analysis"]
                ),
            }
            input_bound = len(canonical(prompt).encode()) + 4096 + 2048
            reserve = (
                Decimal(input_bound) * INPUT_CNY
                + Decimal(chart_provider.MAX_OUTPUT) * OUTPUT_CNY
            ) / 1_000_000
            events = self._events(db)
            calls = [row for row in events if row["kind"] == "MODEL_CALL_RESERVED"]
            spent = sum((Decimal(row["reserved_cny"]) for row in calls), Decimal(0))
            if len(calls) >= MAX_CALLS or spent + reserve > Decimal(
                settings["max_cost_cny"]
            ):
                value.update(status="WAITING_FOR_MODEL", error="CHART_BUDGET_EXHAUSTED")
                self._put(db, value, "MODEL_BUDGET_REFUSED_BEFORE_SEND")
                return True
            try:
                key = self._cipher().decrypt(settings["secret"].encode()).decode()
            except (InvalidToken, ValueError, OSError) as error:
                raise PlatformError("CHART_SECRET_STORE_UNAVAILABLE") from error
            data, mime = self._image_bytes(value)
            call = {
                "request_id": uuid.uuid4().hex,
                "provider": "DeepSeek",
                "model": chart_provider.MODEL,
                "input_token_reserve": input_bound,
                "output_token_reserve": chart_provider.MAX_OUTPUT,
                "reserved_cny": str(reserve),
                "pricing_scope": "CONSERVATIVE_ESTIMATE_NOT_PROVIDER_BILL",
                "prompt_schema_sha256": digest(prompt),
            }
            self._append(db, "MODEL_CALL_RESERVED", job_id=value["id"], **call)
            value.update(status="RUNNING", model_call=call)
            self._put(db, value, "ANALYSIS_STARTED")
        self.runtime.checkpoint("AFTER_RESERVATION")
        failure_stage = "MODEL_REQUEST_OR_RESPONSE"
        try:
            self.runtime.checkpoint("BEFORE_SEND")
            report, usage = self.runtime.analyze(
                key, data, mime, value["request"], value["algorithm_analysis"]
            )
            self.runtime.checkpoint("AFTER_RESPONSE")
            failure_stage = "REPORT_FINALIZATION"
            value.update(
                status="SUCCEEDED",
                report=report.model_dump(mode="json"),
                image_alignment=assess_alignment(value["request"], report),
                model_call={**call, **usage},
                error=None,
            )
            from .chart_conclusions import conclusion

            value["conclusion"] = conclusion(value)
            combined = value["conclusion"]
            algorithm = value["algorithm_analysis"]
            if (
                combined
                and algorithm["status"] == "AVAILABLE"
                and combined["status"] == "READY"
            ):
                from quantforge.strategies.chart_evaluation import freeze_target

                from .chart_timeframes import horizon

                indicators = algorithm["synthesis"]["indicators"]
                value["evaluation_target"] = freeze_target(
                    direction=combined["direction"],
                    timeframe=value["request"]["timeframe"],
                    horizon=horizon(value["request"]),
                    origin=datetime.fromisoformat(
                        algorithm["source"]["last_timestamp"]
                    ),
                    reference=Decimal(indicators["reference_close"]),
                    atr=Decimal(indicators["atr14"]),
                    frozen_at=datetime.now(UTC),
                )
        except chart_provider.UnknownChartCall:
            value.update(status="UNKNOWN", error="CHART_PROVIDER_EFFECT_UNKNOWN")
        except chart_provider.ProviderFailure as error:
            diagnostic = dict(error.diagnostics)
            try:
                directory = self.root / "provider-responses"
                directory.mkdir(mode=0o700, exist_ok=True)
                local_root(directory)
                path = directory / (diagnostic["response_sha256"] + ".response")
                if path.is_symlink():
                    raise OSError("redirected response archive")
                if path.exists():
                    if path.read_bytes() != error.raw_response:
                        raise OSError("response archive differs")
                else:
                    descriptor = os.open(
                        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                    )
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(error.raw_response)
                diagnostic.update(archive_status="SAVED", response_artifact=path.name)
            except (OSError, ValueError):
                diagnostic["archive_status"] = "UNAVAILABLE"
            value.update(
                status="FAILED",
                error=str(error),
                failure_stage=failure_stage,
                provider_diagnostics=diagnostic,
            )
        except (ValueError, OSError, KeyError, TypeError) as error:
            known = str(error) if isinstance(error, PlatformError) else ""
            safe_codes = {
                "CHART_PROVIDER_REJECTED",
                "CHART_PROVIDER_INVALID_REPORT",
                "CHART_PROVIDER_RESPONSE_TOO_LARGE",
            }
            value.update(
                status="FAILED",
                error=known
                if known in safe_codes
                else "CHART_ANALYSIS_FAILED_NO_AUTOMATIC_RETRY",
                failure_stage=failure_stage,
            )
        self.runtime.checkpoint("BEFORE_RESULT_COMMIT")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._read(db, value["id"])
            self._put(db, value, "ANALYSIS_" + value["status"])
        self.runtime.checkpoint("AFTER_RESULT_COMMIT")
        return True
