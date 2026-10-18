"""Contemporaneous source capture and frozen paper journal; no live transport."""

from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Context, Decimal, localcontext
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl
from quantforge.data import BAR_SCHEMA, logical_content_hash, prepare_bars
from quantforge.product.context import source_identity
from quantforge.product.data_usage import DataUsageRegistry, DataWindow
from quantforge.product.fixed_portfolio import replay_fixed_portfolio
from quantforge.product.storage import local_root, write_exclusive, writer_lock

from .data_quality_cli import _frame, fetch_source
from .dataset_use import REGISTRY_ROOT, WORKSPACE
from .execution_cli import load_cash_events
from .long_term_study_cli import declared_policy
from .storage import Ledger, PlatformError, digest

ZONE = ZoneInfo("Asia/Shanghai")
ASSETS = ("159915", "510300", "510500")
WEIGHTS = {key: Decimal("0.3333") for key in ASSETS}


def _now() -> datetime:
    return datetime.now(UTC)


class ForwardPaper:
    def __init__(
        self, root: Path, *, fixture: bool = False, usage_root: Path | None = None
    ) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.root = local_root(root)
        self.ledger = Ledger(self.root)
        self.fixture = fixture
        registry = REGISTRY_ROOT if usage_root is None else usage_root
        registry.mkdir(parents=True, exist_ok=True)
        self.usage = DataUsageRegistry(local_root(registry))

    @property
    def mode(self) -> str:
        return "SOFTWARE_FIXTURE" if self.fixture else "FORWARD_PUBLIC_SOURCE_PAPER"

    def freeze(self) -> dict[str, Any]:
        with writer_lock(self.root):
            history = self.ledger.read()
            if history:
                plan = history[0]["plan"]
                if plan["mode"] != self.mode:
                    raise PlatformError("FORWARD_MODE_IMMUTABLE")
                path = self.root / "plan.json"
                if not path.exists():
                    write_exclusive(path, plan)
                if not (self.root / "source-manifest.json").exists():
                    if source_identity() != plan["core_source_hash"]:
                        raise PlatformError("FORWARD_ORIGINAL_SOURCE_ARCHIVE_REQUIRED")
                    self._archive_sources()
                self._plan(history)
                return dict(plan)
            moment = _now()
            plan = {
                "schema_version": "forward-paper-plan-v1",
                "mode": self.mode,
                "frozen_at": moment.isoformat(),
                "starts_on": (
                    moment.astimezone(ZONE).date() + timedelta(days=1)
                ).isoformat(),
                "weights": {key: str(value) for key, value in WEIGHTS.items()},
                "band": "0.02",
                "policy": declared_policy().to_json(),
                "core_source_hash": source_identity(),
                "host_source_hash": hashlib.sha256(
                    Path(__file__).read_bytes()
                ).hexdigest(),
                "min_elapsed_days": 56,
                "target_elapsed_days": 84,
                "selection": (
                    "SIMPLE_EQUAL_WEIGHT_OPERATIONAL_BASELINE_NOT_WINNER_SELECTION"
                ),
                "live_authority": "NONE",
                "automatic_schedule": False,
                "calendar_qualified": False,
                "default_execution": "PAUSED_UNTIL_CASH_SCOPE_REVIEW",
            }
            program = "forward-" + digest(plan)[:16]
            if not self.fixture:
                admission = self.usage.declare_forward(
                    program=program, protocol_hash=digest(plan)
                )
                plan["registry_starts_on"] = admission["starts_on"]
                plan["registry_protocol_hash"] = digest(
                    {
                        key: value
                        for key, value in plan.items()
                        if key != "registry_starts_on"
                    }
                )
            plan["program"] = program
            with self.ledger.transaction() as db:
                self.ledger.append(db, kind="FORWARD_FROZEN", mode=self.mode, plan=plan)
            write_exclusive(self.root / "plan.json", plan)
            self._archive_sources()
            return plan

    def _archive_sources(self) -> None:
        files = {}
        for repository in ("quantforge", "qf-lab", "qf-platform"):
            root = WORKSPACE / repository
            candidates = list((root / "src").rglob("*.py")) + [
                root / "uv.lock",
                root / "pyproject.toml",
            ]
            for source in candidates:
                relative = source.relative_to(WORKSPACE)
                target = self.root / "source" / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                raw = source.read_bytes()
                if source.is_symlink():
                    raise PlatformError("FORWARD_SOURCE_ARCHIVE_REDIRECTED")
                if target.exists():
                    if target.is_symlink() or target.read_bytes() != raw:
                        raise PlatformError("FORWARD_EXISTING_SOURCE_ARCHIVE_CONFLICT")
                else:
                    with target.open("xb") as stream:
                        stream.write(raw)
                files[relative.as_posix()] = hashlib.sha256(raw).hexdigest()
        write_exclusive(
            self.root / "source-manifest.json",
            {
                "schema_version": "forward-three-package-source-locks-v1",
                "files": files,
                "scope": "PRIVATE_SOURCE_LOCKS_NO_GIT_HEAD_UI_RUNTIME_OR_CREDENTIALS",
            },
        )

    def _plan(self, history: list[dict[str, Any]]) -> dict[str, Any]:
        if not history or history[0]["kind"] != "FORWARD_FROZEN":
            raise PlatformError("FORWARD_FREEZE_REQUIRED")
        plan = history[0]["plan"]
        if plan["mode"] != self.mode:
            raise PlatformError("FORWARD_MODE_IMMUTABLE")
        if json.loads((self.root / "plan.json").read_bytes()) != plan:
            raise PlatformError("FORWARD_PLAN_FILE_MISMATCH")
        manifest = json.loads((self.root / "source-manifest.json").read_bytes())
        for relative, checksum in manifest["files"].items():
            path = self.root / "source" / relative
            local_root(path.parent)
            if (
                not path.resolve().is_relative_to((self.root / "source").resolve())
                or path.is_symlink()
                or hashlib.sha256(path.read_bytes()).hexdigest() != checksum
            ):
                raise PlatformError("FORWARD_FROZEN_SOURCE_ARCHIVE_MISMATCH")
        for row in history:
            for receipt in row.get("receipts", {}).values():
                self._source_bytes(receipt["artifact"], receipt["raw_sha256"])
            if row["kind"] == "FORWARD_CASH_SCOPE_REVIEWED":
                for source in row["coverage"]["reviewed_sources"]:
                    self._source_bytes(source["archive_artifact"], source["sha256"])
        return dict(plan)

    def _source_bytes(self, artifact: str, checksum: str) -> bytes:
        path = self.root / "sources" / artifact
        local_root(path.parent)
        if (
            path.parent.resolve() != (self.root / "sources").resolve()
            or path.is_symlink()
            or path.stat().st_size > 8_000_000
        ):
            raise PlatformError("FORWARD_SOURCE_PATH_OR_SIZE_INVALID")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != checksum:
            raise PlatformError("FORWARD_SOURCE_BYTES_MISMATCH")
        return raw

    def _append(self, kind: str, **payload: Any) -> None:
        with self.ledger.transaction() as db:
            self.ledger.append(db, kind=kind, mode=self.mode, **payload)

    def record_frames(
        self, frames: dict[str, pl.DataFrame], receipts: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        """Host method; production CLI fetches allowlisted HTTPS source bytes."""
        with writer_lock(self.root):
            history = self.ledger.read()
            plan = self._plan(history)
            moment = _now()
            today = moment.astimezone(ZONE).date()
            if (
                today.isoformat()
                < max(
                    plan["starts_on"], plan.get("registry_starts_on", plan["starts_on"])
                )
                or moment.astimezone(ZONE).hour < 16
            ):
                raise PlatformError("FORWARD_FUTURE_SESSION_OR_AFTER_CLOSE_REQUIRED")
            if (
                declared_policy().to_json() != plan["policy"]
                or source_identity() != plan["core_source_hash"]
                or hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
                != plan["host_source_hash"]
            ):
                self._append("FORWARD_PAUSED", reason="FROZEN_EXECUTING_SOURCE_CHANGED")
                raise PlatformError("FORWARD_FROZEN_SOURCE_CHANGED")
            if set(frames) != set(ASSETS) or set(receipts) != set(ASSETS):
                raise PlatformError("FORWARD_COMPLETE_QUOTES_REQUIRED")
            frozen = datetime.fromisoformat(plan["frozen_at"])
            rows = {}
            hashes = {}
            for key, frame in frames.items():
                clean = prepare_bars(frame, as_of=moment)
                if (
                    clean.height != 1
                    or clean["symbol"][0] != key
                    or clean["timestamp"][0].date() != today
                ):
                    raise PlatformError("FORWARD_SAME_DAY_ONE_QUOTE_REQUIRED")
                receipt = receipts[key]
                captured = datetime.fromisoformat(receipt["retrieved_at"])
                if (
                    captured.tzinfo is None
                    or not frozen < captured <= moment
                    or captured.astimezone(ZONE).date() != today
                ):
                    raise PlatformError("FORWARD_CONTEMPORANEOUS_SOURCE_TIME_REQUIRED")
                raw = self._source_bytes(receipt["artifact"], receipt["raw_sha256"])
                if (
                    not self.fixture
                    and receipt["transport"]
                    != "PUBLIC_HTTPS_NOT_EXCHANGE_AUTHENTICATION"
                ):
                    raise PlatformError("FORWARD_PUBLIC_FETCH_RECEIPT_REQUIRED")
                if not self.fixture:
                    data = json.loads(raw)["data"]
                    parsed = _frame(key, [row.split(",") for row in data["klines"]])
                    if str(data["code"]) != key or logical_content_hash(
                        parsed
                    ) != logical_content_hash(clean):
                        raise PlatformError("FORWARD_FRAME_AND_RAW_SOURCE_MISMATCH")
                rows[key] = {
                    **clean.to_dicts()[0],
                    "timestamp": clean["timestamp"][0].isoformat(),
                }
                hashes[key] = logical_content_hash(clean)
            observations = [row for row in history if row["kind"] == "FORWARD_OBSERVED"]
            prior = next(
                (row for row in observations if row["session"] == today.isoformat()),
                None,
            )
            if prior:
                if prior["quote_hashes"] == hashes:
                    self._append(
                        "FORWARD_RECHECK",
                        session=today.isoformat(),
                        receipts=receipts,
                        effect="NO_NEW_ORDER_OR_FILL",
                    )
                    return {"status": "ALREADY_OBSERVED", "new_fills": 0}
                self._append(
                    "FORWARD_REVISION_DETECTED",
                    session=today.isoformat(),
                    previous_hashes=prior["quote_hashes"],
                    revised_hashes=hashes,
                    receipts=receipts,
                )
                raise PlatformError("FORWARD_REVISION_REQUIRES_RECONCILIATION")
            if any(row["kind"] == "FORWARD_REVISION_DETECTED" for row in history):
                raise PlatformError("FORWARD_UNRESOLVED_REVISION")
            if observations and observations[-1]["session"] >= today.isoformat():
                raise PlatformError("FORWARD_CHRONOLOGICAL_SESSIONS_REQUIRED")
            coverage = next(
                (
                    row
                    for row in reversed(history)
                    if row["kind"] == "FORWARD_CASH_SCOPE_REVIEWED"
                    and row["coverage"]["covered_start"]
                    <= today.isoformat()
                    <= row["coverage"]["covered_end"]
                ),
                None,
            )
            executed = [item for item in observations if "period" in item["state"]]
            last_account = executed[-1]["state"]["period"] if executed else None
            state: dict[str, Any] = {
                "status": "PAUSED_CASH_SCOPE_NOT_REVIEWED",
                "orders": [],
                "new_fills": 0,
                "cash": last_account["cash"]
                if last_account
                else plan["policy"]["initial_cash"],
                "last_account_period": last_account,
            }
            if coverage and len(observations) >= 1:
                # Starting later must not fabricate trades on previously paused days.
                start = date.fromisoformat(
                    executed[0]["session"] if executed else today.isoformat()
                )
                if coverage["coverage"]["covered_start"] > start.isoformat():
                    raise PlatformError("FORWARD_COMPLETE_OLD_CASH_SCOPE_REQUIRED")
                seed = next(
                    item
                    for item in reversed(observations)
                    if item["session"] < start.isoformat()
                )
                included = [seed] + [
                    item
                    for item in observations
                    if item["session"] >= start.isoformat()
                ]
                all_rows = {
                    key: [item["rows"][key] for item in included] + [rows[key]]
                    for key in ASSETS
                }
                full_frames = {
                    key: pl.DataFrame(
                        [
                            {
                                **row,
                                "timestamp": datetime.fromisoformat(row["timestamp"]),
                            }
                            for row in values
                        ],
                        schema=BAR_SCHEMA,
                    )
                    for key, values in all_rows.items()
                }
                events = tuple(
                    event
                    for event in load_cash_events(coverage["coverage"])
                    if event.record_date >= start
                )
                replay = replay_fixed_portfolio(
                    full_frames,
                    WEIGHTS,
                    declared_policy(),
                    start_session=start,
                    band=Decimal(plan["band"]),
                    dividends=events,
                )
                old_periods = [
                    item["state"]["period"]
                    for item in observations
                    if "period" in item["state"]
                ]
                if replay["periods"][:-1] != old_periods:
                    self._append(
                        "FORWARD_PAUSED", reason="OLD_PREFIX_RECONCILIATION_MISMATCH"
                    )
                    raise PlatformError("FORWARD_OLD_FACTS_CANNOT_CHANGE")
                new_orders = [
                    order
                    for order in replay["orders"]
                    if order["session"] == today.isoformat()
                ]
                state = {
                    "status": "DECLARED_DAILY_PAPER_NOT_LIVE",
                    "period": replay["periods"][-1],
                    "orders": new_orders,
                    "new_fills": sum(
                        order["status"] in {"FILLED", "PARTIAL"} for order in new_orders
                    ),
                    "intent_visible_from": observations[-1]["captured_at"],
                    "cash_scope_hash": digest(coverage["coverage"]),
                }
            if not self.fixture:
                for key in frames:
                    self.usage.record_forward(
                        DataWindow(
                            "CN:" + key,
                            today,
                            today,
                            hashes[key],
                            cohort="CN_EQUITY_ETF_DECLARED",
                        ),
                        program=plan["program"],
                        source_captured_at=datetime.fromisoformat(
                            receipts[key]["retrieved_at"]
                        ),
                    )
            self._append(
                "FORWARD_OBSERVED",
                session=today.isoformat(),
                captured_at=moment.isoformat(),
                quote_hashes=hashes,
                rows=rows,
                receipts=receipts,
                state=state,
            )
            return {
                "status": state["status"],
                "new_fills": state["new_fills"],
                "session": today.isoformat(),
            }

    def review_cash_scope(self, path: Path) -> None:
        """Explicit host attestation, without corporate-action certification."""
        local_root(path.parent)
        if path.is_symlink():
            raise PlatformError("FORWARD_REGULAR_COVERAGE_REQUIRED")
        value = json.loads(path.read_bytes())
        if (
            value.get("qualification")
            != "HOST_REVIEWED_KNOWN_CASH_SCOPE_NOT_COMPLETE_CORPORATE_ACTIONS"
            or not value.get("reviewed_sources")
        ):
            raise PlatformError("FORWARD_CASH_SCOPE_ATTESTATION_REQUIRED")
        if date.fromisoformat(value["covered_start"]) > date.fromisoformat(
            value["covered_end"]
        ):
            raise PlatformError("FORWARD_CASH_SCOPE_DATES_INVALID")
        load_cash_events(value)
        for item in value["reviewed_sources"]:
            source = path.parent / item["artifact"]
            local_root(source.parent)
            if (
                source.is_symlink()
                or hashlib.sha256(source.read_bytes()).hexdigest() != item["sha256"]
            ):
                raise PlatformError("FORWARD_REVIEWED_SOURCE_MISMATCH")
            archive = self.root / "sources"
            archive.mkdir(exist_ok=True)
            artifact = "cash-review-" + item["sha256"] + ".raw"
            target = archive / artifact
            if target.exists():
                self._source_bytes(artifact, item["sha256"])
            else:
                with target.open("xb") as stream:
                    stream.write(source.read_bytes())
            item["archive_artifact"] = artifact
        with writer_lock(self.root):
            self._plan(self.ledger.read())
            self._append(
                "FORWARD_CASH_SCOPE_REVIEWED",
                coverage=value,
                operator_evidence="HOST_ATTESTATION_NOT_INDEPENDENT_CERTIFICATION",
            )

    def capture(self) -> dict[str, Any]:
        moment = _now()
        today = moment.astimezone(ZONE).date()
        plan = self._plan(self.ledger.read())
        if today.isoformat() < plan["starts_on"] or moment.astimezone(ZONE).hour < 16:
            raise PlatformError("FORWARD_AFTER_CLOSE_FUTURE_SESSION_REQUIRED")
        sources = self.root / "sources"
        sources.mkdir(exist_ok=True)
        frames, receipts = {}, {}
        try:
            for key in ASSETS:
                url = (
                    "https://push2his.eastmoney.com/api/qt/stock/kline/get"
                    f"?secid={0 if key.startswith('1') else 1}.{key}"
                    "&fields1=f1,f2,f3,f4,f5,f6"
                    "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
                    f"&klt=101&fqt=0&beg={today:%Y%m%d}&end={today:%Y%m%d}"
                )
                raw, receipt = fetch_source(
                    url, sources, key + "-" + uuid.uuid4().hex[:8]
                )
                data = json.loads(raw)["data"]
                if data is None or data["code"] != key or not data["klines"]:
                    raise PlatformError("FORWARD_MISSING_CURRENT_QUOTE")
                frames[key] = _frame(key, [row.split(",") for row in data["klines"]])
                receipts[key] = receipt
            return self.record_frames(frames, receipts)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            with writer_lock(self.root):
                self._append(
                    "FORWARD_CAPTURE_PAUSED",
                    session=today.isoformat(),
                    reason=type(exc).__name__,
                    effect="NO_NEW_ORDER_OR_FILL",
                )
            raise PlatformError(
                "FORWARD_CAPTURE_PAUSED_SOURCE_REVIEW_REQUIRED"
            ) from None

    def status(self) -> dict[str, Any]:
        history = self.ledger.read()
        plan = self._plan(history)
        observations = [row for row in history if row["kind"] == "FORWARD_OBSERVED"]
        first = (
            datetime.fromisoformat(observations[0]["captured_at"])
            if observations
            else None
        )
        last = (
            datetime.fromisoformat(observations[-1]["captured_at"])
            if observations
            else None
        )
        elapsed = (last - first).total_seconds() / 86400 if first and last else 0
        execution = sum("period" in row["state"] for row in observations)
        return {
            "schema_version": "forward-paper-status-v1",
            "mode": plan["mode"],
            "program": plan["program"],
            "starts_on": plan["starts_on"],
            "observations": len(observations),
            "execution_sessions": execution,
            "first_capture": first.isoformat() if first else None,
            "last_capture": last.isoformat() if last else None,
            "observed_elapsed_days": elapsed,
            "minimum_time_met": not self.fixture and elapsed >= 56,
            "target_time_met": not self.fixture and elapsed >= 84,
            "calendar_qualified": False,
            "complete": False,
            "pending": [
                "8_TO_12_WEEKS_ACTUAL_OBSERVATION",
                "EXPECTED_SESSION_CALENDAR_AND_GAPS_RECONCILIATION",
                "CASH_EVENT_COVERAGE",
                "INDEPENDENT_ACCOUNT_RECONCILIATION",
            ],
            "pause_events": sum(
                row["kind"]
                in {
                    "FORWARD_PAUSED",
                    "FORWARD_CAPTURE_PAUSED",
                    "FORWARD_REVISION_DETECTED",
                }
                for row in history
            ),
            "paid_model_calls": 0,
            "live_authority": "NONE",
        }

    def monitor(self, expected_sessions: list[str] | None = None) -> dict[str, Any]:
        history = self.ledger.read()
        observations = [row for row in history if row["kind"] == "FORWARD_OBSERVED"]
        executed = [row for row in observations if "period" in row["state"]]
        if expected_sessions is not None:
            if expected_sessions != sorted(set(expected_sessions)):
                raise PlatformError("FORWARD_UNIQUE_SORTED_EXPECTED_SESSIONS_REQUIRED")
            for value in expected_sessions:
                date.fromisoformat(value)
        observed = {row["session"] for row in observations}
        plan = self._plan(history)
        today = _now().astimezone(ZONE).date().isoformat()
        expected = [
            value
            for value in expected_sessions or []
            if plan["starts_on"] <= value <= today
        ]
        orders = [order for row in observations for order in row["state"]["orders"]]
        with localcontext(Context(prec=50)):
            average_exposure = (
                str(
                    sum(
                        (
                            Decimal(row["state"]["period"]["gross_weight"])
                            for row in executed
                        ),
                        Decimal(0),
                    )
                    / len(executed)
                )
                if executed
                else None
            )
            average_cash = (
                str(
                    sum(
                        (
                            Decimal(row["state"]["period"]["cash_fraction"])
                            for row in executed
                        ),
                        Decimal(0),
                    )
                    / len(executed)
                )
                if executed
                else None
            )
            notional = str(
                sum(
                    (
                        Decimal(order["filled"]) * Decimal(order["price"])
                        for order in orders
                    ),
                    Decimal(0),
                )
            )
        return {
            **self.status(),
            "expected_calendar_basis": "OPERATOR_SUPPLIED_NOT_CERTIFIED"
            if expected_sessions is not None
            else "NOT_SUPPLIED",
            "missing_expected_sessions": sorted(set(expected) - observed)
            if expected_sessions is not None
            else None,
            "missing_quote_rate": len(set(expected) - observed) / len(expected)
            if expected
            else None,
            "average_exposure": average_exposure,
            "average_cash_fraction": average_cash,
            "traded_notional": notional,
            "partial_orders": sum(order["status"] == "PARTIAL" for order in orders),
            "rejected_orders": sum(order["status"] == "REJECTED" for order in orders),
            "fee_total": executed[-1]["state"]["period"]["fees"] if executed else "0",
            "return_vs_zero_interest_cash": str(
                Decimal(executed[-1]["state"]["period"]["equity"])
                / Decimal(plan["policy"]["initial_cash"])
                - 1
            )
            if executed
            else None,
            "last_account_period": executed[-1]["state"]["period"]
            if executed
            else None,
            "reconciliation": (
                "OLD_PREFIX_REPLAY_AT_EACH_EXECUTION_NOT_INDEPENDENT_AUDIT"
            ),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=("freeze", "capture", "status", "monitor", "review-cash-scope"),
    )
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--cash-scope", type=Path)
    parser.add_argument("--expected-sessions", type=Path)
    args = parser.parse_args()
    service = ForwardPaper(args.root)
    if args.action == "freeze":
        value = service.freeze()
    elif args.action == "capture":
        value = service.capture()
    elif args.action == "review-cash-scope":
        if args.cash_scope is None:
            parser.error("--cash-scope required")
        service.review_cash_scope(args.cash_scope)
        value = service.status()
    elif args.action == "monitor":
        dates = (
            json.loads(args.expected_sessions.read_bytes())
            if args.expected_sessions
            else None
        )
        value = service.monitor(dates)
    else:
        value = service.status()
    print(json.dumps(value, indent=2))


if __name__ == "__main__":
    main()
