"""Read-only projection of a pinned known-cash/share study and its original PDFs."""

from __future__ import annotations

import base64
import csv
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from quantforge.product.storage import local_root

from .data_quality_cli import _frame
from .dataset_use import register_research_use


class CashShareArchive:
    def __init__(
        self,
        root: Path,
        *,
        plan_hash: str | None,
        result_hash: str | None,
        id: str,
        title: str,
        usage_root: Path | None = None,
    ) -> None:
        self.root, self.id, self.title = root, id, title
        self.plan_hash, self.result_hash, self.usage_root = (
            plan_hash,
            result_hash,
            usage_root,
        )

    def _bytes(self, filename: str) -> bytes:
        if Path(filename).name != filename:
            raise ValueError("ARCHIVE_SOURCE_PATH_INVALID")
        path = self.root / filename
        local_root(path.parent)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 32_000_000:
            raise ValueError("ARCHIVE_SOURCE_INVALID")
        return path.read_bytes()

    def load(self) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        plan_raw, result_raw = self._bytes("plan.json"), self._bytes("result.json")
        if (
            not self.plan_hash
            or not self.result_hash
            or hashlib.sha256(plan_raw).hexdigest() != self.plan_hash
            or hashlib.sha256(result_raw).hexdigest() != self.result_hash
        ):
            raise ValueError("ARCHIVE_PINNED_BYTES_MISMATCH")
        plan, result = json.loads(plan_raw), json.loads(result_raw)
        if (
            result["plan_sha256"] != self.plan_hash
            or result["total_return_qualified"] is not False
            or result["paid_model_calls"] != 0
            or result["live_authority"] != "NONE"
            or plan["final_confirmation"] is not False
            or plan["actual_cent_settlement_qualified"] is not False
        ):
            raise ValueError("ARCHIVE_QUALIFICATION_INVALID")
        catalog_raw = self._bytes("catalog.json")
        if hashlib.sha256(catalog_raw).hexdigest() != plan["catalog_sha256"]:
            raise ValueError("ARCHIVE_CATALOG_MISMATCH")
        catalog = json.loads(catalog_raw)
        for filename, checksum in catalog["sources"].items():
            if hashlib.sha256(self._bytes(filename)).hexdigest() != checksum:
                raise ValueError("ARCHIVE_NOTICE_BYTES_MISMATCH")
        proof = json.loads(self._bytes("independent-check.json"))
        if (
            proof["passed"] is not True
            or proof["result_sha256"] != self.result_hash
            or proof["total_return_qualified"] is not False
        ):
            raise ValueError("ARCHIVE_INDEPENDENT_CHECK_MISMATCH")
        return plan, result, catalog

    def register(self, plan: dict[str, Any]) -> None:
        raw = self._bytes("510500-raw.csv")
        if hashlib.sha256(raw).hexdigest() != plan["csv_sha256"]:
            raise ValueError("ARCHIVE_DATA_SOURCE_MISMATCH")
        rows = list(csv.DictReader(raw.decode().splitlines()))
        frame = _frame(
            "510500",
            [
                [r[k] for k in ("date", "open", "close", "high", "low", "volume")]
                for r in rows
            ],
        )
        register_research_use(
            frame,
            program="archive-view-" + self.id,
            reference_hash=self.result_hash or "",
            registry_root=self.usage_root,
        )

    def header(self, plan: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        periods = result["results"]["cash_and_share"]["single"]["periods"]
        return {
            "id": self.id,
            "title": self.title,
            "kind": "CASH_SHARE",
            "mode": "PAPER_RESEARCH",
            "basis": "RAW_PLUS_KNOWN_GROSS_CASH_AND_SHARES",
            "assets": ["510500"],
            "scenarios": [
                {"key": name, "fee_bps": None, "extra_delay_bars": 0}
                for name in ("buy_hold", "single", "shared")
            ],
            "plan_hash": self.plan_hash,
            "result_hash": self.result_hash,
            "core_source_hash": plan["core_source_hash"],
            "formal_evidence_pack": False,
            "validated_alpha": False,
            "live_authority": "NONE",
            "start": periods[0]["session"],
            "end": periods[-1]["session"],
        }

    def artifact_map(self, catalog: dict[str, Any]) -> dict[str, str]:
        result = {
            "plan": "plan.json",
            "result": "result.json",
            "catalog": "catalog.json",
            "proof": "independent-check.json",
        }
        asset = catalog["assets"]["510500"]
        names = [event["archive_artifact"] for event in asset["cash_events"]] + [
            asset["split_result_confirmation"]["artifact"]
        ]
        for filename in names:
            checksum = catalog["sources"][filename]
            result["source-" + checksum] = filename
        return result

    def artifact(self, name: str) -> dict[str, Any]:
        plan, _, catalog = self.load()
        self.register(plan)
        files = self.artifact_map(catalog)
        if name not in files:
            raise ValueError("ARCHIVE_ARTIFACT_NOT_REGISTERED")
        raw = self._bytes(files[name])
        binary = name.startswith("source-")
        filename = name + ".pdf" if binary else files[name]
        return {
            "schema_version": "qf-archive-artifact-v2",
            "id": self.id,
            "artifact_id": name,
            "filename": filename,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "content": base64.b64encode(raw).decode()
            if binary
            else raw.decode("utf-8"),
            "encoding": "base64" if binary else "utf-8",
            "mime": "application/pdf" if binary else "application/json",
            "live_authority": "NONE",
        }

    def get(self, *, asset: str | None, scenario: str | None) -> dict[str, Any]:
        plan, result, catalog = self.load()
        selected = scenario or "single"
        if asset not in {None, "510500"} or selected not in {
            "buy_hold",
            "single",
            "shared",
        }:
            raise ValueError("ARCHIVE_GROUP_UNAVAILABLE")
        self.register(plan)
        rows = []
        claims = []
        for arm in ("share_only", "cash_and_share"):
            report = result["results"][arm][selected]
            periods = report["periods"]
            initial = (
                Decimal(report["initial_equity"])
                if selected == "buy_hold"
                else Decimal(plan["policy"]["initial_cash"])
            )
            output = []
            peak, worst = initial, Decimal(0)
            for period in periods:
                equity = Decimal(period["equity"])
                peak = max(peak, equity)
                worst = min(worst, equity / peak - 1)
                cash, receivable = period["cash"], period["receivable"]
                position = (equity - Decimal(cash) - Decimal(receivable)) / equity
                quantity = (
                    period["shares_after_close"]
                    if selected == "buy_hold"
                    else period["quantity"]
                    if selected == "single"
                    else period["quantities"]["510500"]
                )
                mark_basis = (
                    period["mark_basis"]["510500"]
                    if selected == "shared"
                    else period["mark_basis"]
                )
                output.append(
                    {
                        "date": period["session"],
                        "equity": float(equity),
                        "position": float(position),
                        "quantity": quantity,
                        "cash": cash,
                        "fees": period.get("fees", "0"),
                        "receivable": receivable,
                        "gross_distributions": period["cash"]
                        if selected == "buy_hold"
                        else period["dividend_cash"],
                        "mark_basis": mark_basis,
                    }
                )
            gross = (
                report["cash_book"]["cash"]
                if selected == "buy_hold"
                else periods[-1]["dividend_cash"]
            )
            claims.append(
                {
                    "strategy": arm.upper(),
                    "gross_distributions": gross,
                    "actual_settlement_verified": False,
                }
            )
            rows.append(
                {
                    "strategy": arm.upper(),
                    "metrics": {
                        "total_return": float(
                            Decimal(periods[-1]["equity"]) / initial - 1
                        ),
                        "max_drawdown": float(worst),
                        "turnover": None,
                    },
                    "exposure": sum(p["position"] for p in output) / len(output),
                    "cash_fraction": sum(
                        float(Decimal(p["cash"]) / Decimal(str(p["equity"])))
                        for p in output
                    )
                    / len(output),
                    "flat_up_day_fraction": None,
                    "fee_total": report.get("fee_total", "0"),
                    "fill_count": report.get(
                        "fill_count",
                        sum(
                            order["status"] in {"FILLED", "PARTIAL"}
                            for order in report.get("orders", [])
                        ),
                    ),
                    "weight_breach_days": report.get(
                        "market_weight_breach_days",
                        report.get("gross_cap_breach_days", 0),
                    ),
                    "periods": output,
                }
            )
        artifacts = []
        for name, filename in self.artifact_map(catalog).items():
            raw = self._bytes(filename)
            artifacts.append(
                {
                    "id": name,
                    "filename": name + ".pdf"
                    if name.startswith("source-")
                    else filename,
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "encoding": "base64" if name.startswith("source-") else "utf-8",
                }
            )
        header = self.header(plan, result)
        header["start"] = rows[0]["periods"][0]["date"]
        return {
            **header,
            "schema_version": "qf-archive-report-v2",
            "asset": "510500",
            "scenario": selected,
            "rows": rows,
            "gross_claims": claims,
            "actual_cent_settlement_qualified": False,
            "cash_events": [
                {
                    key: event[key]
                    for key in (
                        "event_id",
                        "published_date",
                        "record_date",
                        "ex_date",
                        "pay_date",
                        "amount_per_share",
                        "source_url",
                        "source_sha256",
                    )
                }
                for event in catalog["assets"]["510500"]["cash_events"]
            ],
            "source": {
                "declaration": "原始510500历史CSV及上交所现金/拆分公告，"
                "独立8808会话核对。",
                "adjustment": "原价格＋已知份额事件＋税前理论权益；"
                "入账取整/税费/托管份额未核验。",
                "prior_exposure": "完整历史区间已暴露DEVELOPMENT，不是前向或最终确认。",
            },
            "limitations": [
                "总回报、实际入账及完整公司行动资格未授予。",
                "收盘后单位标记为理论换算；不是新的观察成交价。",
                "买持不计交易费，单账户/共享账户使用原声明费用。",
                "不同账户起点/执行语义不同，不跨情景合并排名。",
            ],
            "artifacts": artifacts,
            "decisions": [],
            "session_diagnostics": {},
            "data_usage_registered": True,
            "total_return_qualified": False,
            "observed_at": datetime.now(UTC).isoformat(),
        }
