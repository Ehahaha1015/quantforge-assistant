"""Host-verified 510300 cash-event reconstruction; raw quotes, no reinvestment."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import uuid
from datetime import date
from decimal import Context, Decimal, localcontext
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import polars as pl
from quantforge.product.context import code_context, source_identity
from quantforge.product.dividend_accounting import CashDividend, buy_hold_cash_return
from quantforge.product.storage import local_root, write_exclusive

from .data_quality_cli import fetch_source
from .storage import digest

_INDEX = "https://fundf10.eastmoney.com/fhsp_510300.html"
_SECOND_INDEX = (
    "https://stock.finance.sina.com.cn/fundInfo/view/FundInfo_JJFH.php?symbol=510300"
)
_SSE = "https://www.sse.com.cn/disclosure/fund/announcement/c/new/"
_EVENTS = (
    (
        "2021-01-11",
        "2021-01-15",
        "2021-01-18",
        "2021-01-21",
        "0.072",
        "https://pdf.dfcfw.com/pdf/H2_AN202101111449474410_1.pdf",
    ),
    (
        "2022-01-12",
        "2022-01-18",
        "2022-01-19",
        "2022-01-24",
        "0.075",
        "https://pdf.dfcfw.com/pdf/H2_AN202201121539913641_1.pdf?1642005216000.pdf=",
    ),
    (
        "2023-01-09",
        "2023-01-13",
        "2023-01-16",
        "2023-01-19",
        "0.064",
        _SSE + "2023-01-09/510300_20230109_0ED5.pdf",
    ),
    (
        "2024-01-11",
        "2024-01-17",
        "2024-01-18",
        "2024-01-23",
        "0.069",
        "https://vip.stock.finance.sina.com.cn/fund/go.php/vAkFundInfo_JJGKXX/q/3644514.phtml",
    ),
    (
        "2025-06-11",
        "2025-06-17",
        "2025-06-18",
        "2025-06-27",
        "0.088",
        _SSE + "2025-06-11/510300_20250611_ZAU4.pdf",
    ),
    (
        "2026-01-12",
        "2026-01-16",
        "2026-01-19",
        "2026-01-27",
        "0.123",
        _SSE + "2026-01-12/510300_20260112_VTCZ.pdf",
    ),
)


class Tables(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = []

    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self.cell is not None and self.row is not None:
            self.row.append("".join(self.cell).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None


def _index_events(raw: bytes) -> list[dict[str, str]]:
    parser = Tables()
    parser.feed(raw.decode("utf-8"))
    result = []
    for row in parser.rows:
        if len(row) < 5:
            continue
        amount = re.fullmatch(r"每10份派现金([0-9.]+)元", row[3])
        if amount is None:
            continue
        record, ex, pay = (date.fromisoformat(row[index]) for index in (1, 2, 4))
        if not date(2020, 1, 1) <= record <= date(2026, 9, 30):
            continue
        with localcontext(Context(prec=50)):
            per_share = str((Decimal(amount[1]) / 10).normalize())
        result.append(
            {
                "record": record.isoformat(),
                "ex": ex.isoformat(),
                "pay": pay.isoformat(),
                "amount": per_share,
            }
        )
    return sorted(result, key=lambda row: row["record"])


def run_dividends(bars_path: Path, output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    base = local_root(output)
    root = base / ("run-" + uuid.uuid4().hex[:12])
    root.mkdir()
    if bars_path.is_symlink() or not bars_path.is_file():
        raise ValueError("regular raw-bar parquet required")
    frame = pl.read_parquet(bars_path)
    if frame["symbol"].unique().to_list() != ["510300"]:
        raise ValueError("510300 raw historical bars required")
    raw, index = fetch_source(_INDEX, root, "cash-event-index")
    _, second = fetch_source(_SECOND_INDEX, root, "cash-event-second-index")
    listed = _index_events(raw)
    expected = [
        {"record": record, "ex": ex, "pay": pay, "amount": amount}
        for _, record, ex, pay, amount, _ in _EVENTS
    ]
    if listed != expected:
        raise ValueError("cash-event index differs from the verified declared list")
    events = []
    receipts = []
    for published, record, ex, pay, amount, url in _EVENTS:
        raw, receipt = fetch_source(url, root, "notice-" + record)
        if url.endswith(".pdf") and not raw.startswith(b"%PDF"):
            raise ValueError("cash announcement is not a PDF")
        receipts.append(
            {
                **receipt,
                "verification": "HOST_READ_PRIMARY_CONTENT",
                "published_date": published,
                "publisher": "HUATAI_PINEBRIDGE",
                "transport_source": "PRIMARY_TEXT_MIRROR"
                if "sina.com.cn" in url
                else "PRIMARY_PDF_OR_MIRROR",
            }
        )
        events.append(
            CashDividend(
                "510300-" + ex,
                "510300",
                date.fromisoformat(published),
                date.fromisoformat(record),
                date.fromisoformat(ex),
                date.fromisoformat(pay),
                Decimal(amount),
                url,
                receipt["raw_sha256"],
                True,
            )
        )
    plan = {
        "schema_version": "cash-event-reconstruction-plan-v1",
        "symbol": "510300",
        "price_basis": "RAW_UNADJUSTED",
        "shares": "1000",
        "events": [event.to_json() for event in events],
        "source_file_hash": hashlib.sha256(bars_path.read_bytes()).hexdigest(),
        "source_path": str(bars_path.absolute()),
        "enumeration": [index, second],
        "notices": receipts,
        "core_context": code_context(),
        "core_source_hash": source_identity(),
        "coverage_assurance": "ENUMERATED_KNOWN_PRIMARY_CASH_EVENTS",
        "full_corporate_action_history_certified": False,
    }
    write_exclusive(root / "plan.json", plan)
    result = buy_hold_cash_return(
        frame,
        tuple(events),
        shares=Decimal(1000),
        coverage_verified=False,
    )
    result.update(
        plan_id=digest(plan),
        coverage_assurance="KNOWN_CASH_EVENTS_RECONSTRUCTED_FULL_OTHER_ACTIONS_UNCERTIFIED",
    )
    write_exclusive(root / "result.json", result)
    write_exclusive(root / "result-digest.json", {"sha256": digest(result)})
    (root / "report.md").write_text(
        "\n".join(
            [
                "# DATA-LT-002: raw price and cash-event reconstruction",
                "",
                "Declared 1000 shares; no reinvestment or trading authority.",
                f"Initial equity: {result['initial_equity']} CNY.",
                f"Price-only return: {result['price_return']}.",
                f"Known cash-event total return: {result['known_cash_total_return']}.",
                f"Cash paid: {result['dividend_cash']}; "
                f"receivable: {result['receivable']}.",
                "Events are source-bound; entitlement uses record-close quantity.",
                "Ex-date receivable becomes payment-date cash; never counted twice.",
                "Adjusted prices refused; full action coverage separately qualified.",
                "",
                "| Record | Ex | Pay | Cash/share |",
                "| --- | --- | --- | ---: |",
                *[
                    f"| {event.record_date} | {event.ex_date} | {event.pay_date} "
                    f"| {event.amount_per_share} |"
                    for event in events
                ],
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "artifact_root": str(root),
        "cash": result["dividend_cash"],
        "receivable": result["receivable"],
        "known_events": len(events),
        "price_return": result["price_return"],
        "known_cash_total_return": result["known_cash_total_return"],
        "total_return_qualified": result["total_return_qualified"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run_dividends(args.bars, args.out), indent=2))


if __name__ == "__main__":
    main()
