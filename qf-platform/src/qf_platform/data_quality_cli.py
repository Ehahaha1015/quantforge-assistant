"""Refresh immutable public-source vintages and audit three declared ETF datasets."""

from __future__ import annotations

import argparse
import hashlib
import json
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import polars as pl
from quantforge.data import BAR_SCHEMA
from quantforge.product.context import code_context, source_identity
from quantforge.product.data_quality import (
    MarketSource,
    SessionEvidence,
    audit_bars,
    compare_prices,
)
from quantforge.product.storage import local_root, write_exclusive

from .storage import digest

_HOSTS = {
    "push2his.eastmoney.com",
    "web.ifzq.gtimg.cn",
    "pdf.dfcfw.com",
    "www.cindasc.com",
    "www.sse.com.cn",
    "static.cninfo.com.cn",
    "fundf10.eastmoney.com",
    "vip.stock.finance.sina.com.cn",
    "stock.finance.sina.com.cn",
}
_KLINE = (
    "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    "?secid={market}.{symbol}&fields1=f1,f2,f3,f4,f5,f6"
    "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
    "&klt=101&fqt=0&beg=20200101&end=20260930"
)
_SUSPENSION = "https://pdf.dfcfw.com/pdf/H2_AN202102081459963456_1.pdf"
_BROKER_NOTICE = "https://www.cindasc.com/osoa/views/a/20210209/42396.html"
_TENCENT = (
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    "?param=sz159915,day,2021-02-01,2021-02-12,30,"
)


def fetch_source(url: str, root: Path, label: str) -> tuple[bytes, dict[str, Any]]:
    if urlsplit(url).scheme != "https" or urlsplit(url).hostname not in _HOSTS:
        raise ValueError("allowlisted public HTTPS source required")
    with urlopen(
        Request(url, headers={"User-Agent": "QuantForge-research/0.1"}), timeout=20
    ) as response:
        if response.status != 200 or response.geturl() != url:
            raise ValueError("unexpected public-source response")
        raw = response.read(8_000_001)
        if not raw or len(raw) > 8_000_000:
            raise ValueError("public source exceeds bounds")
    identity = hashlib.sha256(raw).hexdigest()
    target = root / (label + "-" + identity[:16] + ".raw")
    with target.open("xb") as stream:
        stream.write(raw)
    return raw, {
        "provider_url": url,
        "raw_sha256": identity,
        "retrieved_at": datetime.now(UTC).isoformat(),
        "artifact": target.name,
        "transport": "PUBLIC_HTTPS_NOT_EXCHANGE_AUTHENTICATION",
    }


def _frame(symbol: str, rows: list[list[Any]]) -> pl.DataFrame:
    selected = []
    for row in rows:
        if len(row) < 6:
            raise ValueError("OHLCV source row required")
        day = date.fromisoformat(str(row[0]))
        open_, close, high, low, volume = (float(row[index]) for index in range(1, 6))
        selected.append(
            {
                "symbol": symbol,
                "timestamp": datetime(day.year, day.month, day.day, tzinfo=UTC),
                "open": open_,
                "close": close,
                "high": high,
                "low": low,
                "volume": volume,
            }
        )
    return pl.DataFrame(selected, schema=BAR_SCHEMA)


def run_quality(output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    base = local_root(output)
    root = base / (
        "vintage-"
        + datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        + "-"
        + uuid.uuid4().hex[:8]
    )
    root.mkdir()
    sources = {}
    frames = {}
    for symbol in ("510300", "510500", "159915"):
        url = _KLINE.format(market=0 if symbol.startswith("1") else 1, symbol=symbol)
        raw, receipt = fetch_source(url, root, symbol + "-eastmoney")
        payload = json.loads(raw)
        data = payload["data"]
        if str(data["code"]) != symbol:
            raise ValueError("provider symbol mismatch")
        frames[symbol] = _frame(symbol, [row.split(",") for row in data["klines"]])
        sources[symbol] = receipt
    suspension, document = fetch_source(_SUSPENSION, root, "159915-suspension")
    if not suspension.startswith(b"%PDF"):
        raise ValueError("issuer announcement must be a PDF")
    _, broker = fetch_source(_BROKER_NOTICE, root, "159915-resumption")
    raw, secondary_source = fetch_source(_TENCENT, root, "159915-tencent")
    values = json.loads(raw)["data"]["sz159915"]
    if "day" not in values:
        raise ValueError("unadjusted secondary daily data required")
    secondary = _frame("159915", values["day"])
    proofs = (
        SessionEvidence(
            "159915",
            date(2021, 2, 8),
            "DOCUMENTED_SUSPENSION",
            _SUSPENSION,
            document["raw_sha256"],
            date(2021, 2, 8),
        ),
        SessionEvidence(
            "159915",
            date(2021, 2, 9),
            "PARTIAL_SESSION",
            _BROKER_NOTICE,
            broker["raw_sha256"],
            date(2021, 2, 9),
        ),
    )
    union = sorted(
        {
            value.date().isoformat()
            for frame in frames.values()
            for value in frame["timestamp"]
        }
    )
    reports = {}
    for symbol, frame in frames.items():
        receipt = sources[symbol]
        reports[symbol] = audit_bars(
            frame,
            symbol=symbol,
            source=MarketSource(
                "EAST_MONEY",
                receipt["provider_url"],
                datetime.fromisoformat(receipt["retrieved_at"]),
                receipt["raw_sha256"],
                "RAW_UNADJUSTED",
                "LOTS",
                "VENDOR_RESPONSE",
            ),
            reference_sessions=union,
            session_evidence=proofs,
        )
        frame.write_parquet(root / (symbol + "-bars.parquet"))
    days = set(value.date().isoformat() for value in secondary["timestamp"])
    primary = frames["159915"].filter(
        pl.col("timestamp").dt.date().is_between(date(2021, 2, 1), date(2021, 2, 12))
    )
    comparison = compare_prices(primary, secondary)
    result = {
        "schema_version": "data-quality-inventory-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "core_context": code_context(),
        "core_source_hash": source_identity(),
        "sources": sources,
        "assets": reports,
        "documents": {"suspension": document, "resumption_secondary": broker},
        "second_source": {"receipt": secondary_source, "comparison": comparison},
        "gap_check": {
            "symbol": "159915",
            "session": "2021-02-08",
            "primary_has_bar": "2021-02-08"
            in set(primary["timestamp"].dt.date().cast(pl.String)),
            "secondary_has_bar": "2021-02-08" in days,
            "classification": "DOCUMENTED_SUSPENSION",
            "intraday_execution_qualified": False,
        },
        "exposure_declaration": "All complete intervals are prior exposed "
        "exploratory research data.",
        "total_return_qualified": False,
        "live_eligible": False,
    }
    write_exclusive(root / "inventory.json", result)
    write_exclusive(root / "inventory-digest.json", {"sha256": digest(result)})
    lines = [
        "# DATA-LT-001: public-source data quality",
        "",
        "Quotes remain vendor data, not exchange-authenticated execution.",
        "| Symbol | Rows | Status | Missing dates |",
        "| --- | ---: | --- | --- |",
        *[
            f"| {symbol} | {value.get('observations', 0)} | {value['status']} | "
            f"{value.get('missing_sessions', [])} |"
            for symbol, value in reports.items()
        ],
        "",
        "159915 2021-02-08: documented issuer suspension. No fabricated bar.",
        "2021-02-09 is partial; exact open execution remains unqualified.",
        "Issuer PDF and broker notice were independently read by the host.",
        f"Cross-source: {comparison['status']}; "
        f"{comparison['overlap_sessions']} shared sessions.",
        "Date-only publication does not attest availability before an open.",
    ]
    (root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {
        "artifact_root": str(root),
        "gap_check": result["gap_check"],
        "comparison": comparison["status"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run_quality(args.out), indent=2))


if __name__ == "__main__":
    main()
