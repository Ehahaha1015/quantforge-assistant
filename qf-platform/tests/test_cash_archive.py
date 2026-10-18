import base64
import hashlib
import json
from pathlib import Path

import pytest
from quantforge.product.data_usage import DataUsageRegistry

from qf_platform.data_quality_cli import _frame
from qf_platform.dataset_use import window_of
from qf_platform.research_archive import ArchiveSource, ResearchArchive
from qf_platform.storage import canonical


def fixture(root: Path) -> tuple[ResearchArchive, Path, dict[str, str]]:
    run = root / "synthetic-not-market"
    run.mkdir()
    pdf = b"%PDF-1.4\nSYNTHETIC SOURCE HASH TEST ONLY"
    sha = hashlib.sha256(pdf).hexdigest()
    (run / "notice.pdf").write_bytes(pdf)
    sources = {"notice.pdf": sha}
    events = [
        {
            "event_id": f"test-{i}",
            "published_date": "2019-01-01",
            "record_date": "2019-01-02",
            "ex_date": "2019-01-03",
            "pay_date": "2019-01-04",
            "amount_per_share": "0.01",
            "source_url": "https://www.sse.com.cn/disclosure/fund/announcement/test.pdf",
            "source_sha256": sha,
            "archive_artifact": "notice.pdf",
        }
        for i in range(4)
    ]
    catalog = {
        "sources": sources,
        "assets": {
            "510500": {
                "cash_events": events,
                "split_result_confirmation": {"artifact": "notice.pdf"},
            }
        },
    }
    catalog_raw = canonical(catalog).encode()
    (run / "catalog.json").write_bytes(catalog_raw)
    csv = (
        b"date,open,high,low,close,volume\n"
        b"2020-01-01,10,10,10,10,1000\n"
        b"2020-01-02,10,10,10,10,1000\n"
    )
    (run / "510500-raw.csv").write_bytes(csv)
    plan = {
        "core_source_hash": "a" * 64,
        "policy": {"initial_cash": "1000"},
        "final_confirmation": False,
        "actual_cent_settlement_qualified": False,
        "catalog_sha256": hashlib.sha256(catalog_raw).hexdigest(),
        "csv_sha256": hashlib.sha256(csv).hexdigest(),
    }
    plan_raw = canonical(plan).encode()
    (run / "plan.json").write_bytes(plan_raw)
    groups = {}
    for arm in ("share_only", "cash_and_share"):
        periods = [
            {
                "session": f"2020-01-0{i + 1}",
                "equity": "1000",
                "cash": "900",
                "receivable": "0",
                "quantity": "10",
                "quantities": {"510500": "10"},
                "mark": "10",
                "marks": {"510500": "10"},
                "fees": "0",
                "dividend_cash": "0.123" if arm == "cash_and_share" else "0",
                "mark_basis": "OBSERVED_RAW_CLOSE",
            }
            for i in range(2)
        ]
        holding = [{**p, "shares_after_close": "10"} for p in periods]
        shared = [
            {**p, "mark_basis": {"510500": "OBSERVED_RAW_CLOSE"}} for p in periods
        ]
        groups[arm] = {
            "single": {"periods": periods},
            "shared": {
                "periods": shared,
                "orders": [{"status": "REJECTED"}, {"status": "PARTIAL"}],
            },
            "buy_hold": {
                "periods": holding,
                "initial_equity": "1000",
                "cash_book": {"cash": "0.123" if arm == "cash_and_share" else "0"},
            },
        }
    result = {
        "plan_sha256": hashlib.sha256(plan_raw).hexdigest(),
        "results": groups,
        "total_return_qualified": False,
        "paid_model_calls": 0,
        "live_authority": "NONE",
    }
    result_raw = canonical(result).encode()
    (run / "result.json").write_bytes(result_raw)
    (run / "independent-check.json").write_text(
        json.dumps(
            {
                "passed": True,
                "result_sha256": hashlib.sha256(result_raw).hexdigest(),
                "total_return_qualified": False,
            }
        )
    )
    source = ArchiveSource(
        "synthetic-cash",
        "Synthetic fixture only",
        "CASH_SHARE",
        run,
        (),
        hashlib.sha256(plan_raw).hexdigest(),
        hashlib.sha256(result_raw).hexdigest(),
    )
    return ResearchArchive((source,), usage_root=root / "usage"), run, sources


def test_exact_pinned_projection_and_binary_download(tmp_path: Path) -> None:
    service, run, sources = fixture(tmp_path)
    report = service.get("synthetic-cash", scenario="single")
    assert report["schema_version"] == "qf-archive-report-v2"
    assert report["gross_claims"][1]["gross_distributions"] == "0.123"
    assert not report["actual_cent_settlement_qualified"]
    assert report["basis"] == "RAW_PLUS_KNOWN_GROSS_CASH_AND_SHARES"
    assert (
        service.get("synthetic-cash", scenario="shared")["rows"][0]["fill_count"] == 1
    )
    sha = sources["notice.pdf"]
    artifact = service.artifact("synthetic-cash", "source-" + sha)
    assert artifact["encoding"] == "base64"
    assert base64.b64decode(artifact["content"]) == (run / "notice.pdf").read_bytes()
    with pytest.raises(ValueError):
        service.artifact("synthetic-cash", "../notice.pdf")


def test_changed_result_even_with_new_sidecar_is_not_displayed(tmp_path: Path) -> None:
    service, run, _ = fixture(tmp_path)
    result = json.loads((run / "result.json").read_bytes())
    result["results"]["cash_and_share"]["single"]["periods"][0]["equity"] = "999999"
    (run / "result.json").write_text(canonical(result))
    (run / "result-digest.json").write_text(
        json.dumps({"sha256": hashlib.sha256(canonical(result).encode()).hexdigest()})
    )
    assert service.list()["items"] == []
    with pytest.raises(ValueError, match="PINNED"):
        service.get("synthetic-cash")


def test_missing_or_changed_notice_blocks_projection_and_artifacts(
    tmp_path: Path,
) -> None:
    service, run, _ = fixture(tmp_path)
    (run / "notice.pdf").write_bytes(b"changed source")
    with pytest.raises(ValueError, match="NOTICE_BYTES"):
        service.get("synthetic-cash")
    with pytest.raises(ValueError, match="NOTICE_BYTES"):
        service.artifact("synthetic-cash", "result")


def test_reserved_interval_blocks_new_projection_and_all_downloads(
    tmp_path: Path,
) -> None:
    service, _, _ = fixture(tmp_path)
    directory = tmp_path / "usage"
    directory.mkdir()
    bars = _frame(
        "510500",
        [["2020-01-01", 10, 10, 10, 10, 1000], ["2020-01-02", 10, 10, 10, 10, 1000]],
    )
    DataUsageRegistry(directory).reserve_final(
        window_of(bars), program="final-fixture", protocol_hash="f" * 64
    )
    with pytest.raises(ValueError):
        service.get("synthetic-cash")
    with pytest.raises(ValueError):
        service.artifact("synthetic-cash", "plan")
