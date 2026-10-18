from __future__ import annotations

import hashlib
import http.client
import json
import threading
from pathlib import Path

import polars as pl
import pytest
from quantforge.product.data_usage import DataUsageRegistry
from test_weight_cli import sources

from qf_platform.dataset_use import window_of
from qf_platform.research_archive import ArchiveSource, ResearchArchive
from qf_platform.server import make_server
from qf_platform.storage import digest
from qf_platform.weight_cli import run_weights


def archive(root: Path) -> tuple[ResearchArchive, Path]:
    inventory, dividend = sources(root)
    output = run_weights(
        inventory,
        dividend,
        root / "out",
        history_bars=20,
        usage_root=root / "research-use",
    )
    run = Path(output["artifact_root"])
    return ResearchArchive(
        (
            ArchiveSource(
                "test-weights", "Test", "WEIGHTS", run, (root / "510300-bars.parquet",)
            ),
        ),
        usage_root=root / "view-use",
    ), run


def test_verified_projection_and_exact_original_artifact(tmp_path: Path) -> None:
    service, run = archive(tmp_path)
    assert not service.list()["issues"]
    report = service.get("test-weights")
    assert (
        report["basis"] == "RAW_PLUS_KNOWN_CASH" and report["mode"] == "PAPER_RESEARCH"
    )
    assert report["data_usage_registered"] and not report["formal_evidence_pack"]
    assert not report["total_return_qualified"] and report["live_authority"] == "NONE"
    assert len(report["rows"]) == 6 and len(report["rows"][0]["periods"]) == 60
    assert report["rows"][0]["periods"][0]["cash"] is not None
    result = service.artifact("test-weights", "result")
    assert result["content"].encode() == (run / "result.json").read_bytes()
    assert hashlib.sha256(result["content"].encode()).hexdigest() == result["sha256"]
    with pytest.raises(ValueError):
        service.get("../../private")
    with pytest.raises(ValueError):
        service.artifact("test-weights", "../bindings")
    with pytest.raises(ValueError):
        service.get("test-weights", scenario="10-0")


def test_result_or_original_data_mutation_is_not_displayed(tmp_path: Path) -> None:
    service, run = archive(tmp_path)
    path = run / "result.json"
    original = path.read_text()
    value = json.loads(original)
    value["scenarios"]["CASH"]["net_return"] = "9"
    path.write_text(json.dumps(value))
    assert service.list()["items"] == [] and service.list()["issues"]
    with pytest.raises(ValueError, match="HASH"):
        service.get("test-weights")
    path.write_text(original)
    bars = pl.read_parquet(tmp_path / "510300-bars.parquet")
    bars.with_columns(pl.col("volume") + 1).write_parquet(
        tmp_path / "510300-bars.parquet"
    )
    with pytest.raises(ValueError, match="SOURCE_MISMATCH"):
        service.get("test-weights")


def test_final_reserved_window_blocks_view_and_full_result_download(
    tmp_path: Path,
) -> None:
    service, _ = archive(tmp_path)
    window = window_of(pl.read_parquet(tmp_path / "510300-bars.parquet"))
    registry_root = tmp_path / "view-use"
    registry_root.mkdir()
    registry = DataUsageRegistry(registry_root)
    registry.reserve_final(window, program="final", protocol_hash="f" * 64)
    with pytest.raises(ValueError):
        service.get("test-weights")
    with pytest.raises(ValueError):
        service.artifact("test-weights", "result")


def test_false_qualification_remains_required_even_with_updated_checksum(
    tmp_path: Path,
) -> None:
    service, run = archive(tmp_path)
    value = json.loads((run / "result.json").read_text())
    value["live_eligible"] = True
    (run / "result.json").write_text(json.dumps(value))
    (run / "result-digest.json").write_text(json.dumps({"sha256": digest(value)}))
    with pytest.raises(ValueError, match="QUALIFICATION"):
        service.get("test-weights")


def test_http_archive_routes_remain_read_only_and_source_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _ = archive(tmp_path)
    monkeypatch.setattr("qf_platform.server.ResearchArchive", lambda: service)
    root = tmp_path / "runtime"
    root.mkdir()
    server = make_server(root, 0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:

        def request(
            path: str, *, origin: str | None = None, post: bool = False
        ) -> tuple[int, dict[str, object]]:
            client = http.client.HTTPConnection(
                "127.0.0.1", server.server_port, timeout=10
            )
            try:
                headers = {"Content-Type": "application/json", "X-QF-Mode": "RESEARCH"}
                if origin:
                    headers["Origin"] = origin
                client.request(
                    "POST" if post else "GET", path, "{}" if post else None, headers
                )
                response = client.getresponse()
                return response.status, json.loads(response.read())
            finally:
                client.close()

        status, value = request("/api/research-archive")
        assert status == 200 and value["live_authority"] == "NONE"
        status, report = request(
            "/api/research-archive/test-weights?asset=510300&scenario=declared"
        )
        assert status == 200 and report["data_usage_registered"] is True
        assert request("/api/research-archive/test-weights/artifacts/plan")[0] == 200
        for path in (
            "/api/research-archive?root=private",
            "/api/research-archive/test-weights?asset=510300&asset=510500",
            "/api/research-archive/unregistered",
            "/api/research-archive/test-weights/artifacts/bindings",
        ):
            assert request(path)[0] == 409
        assert (
            request("/api/research-archive", origin="https://untrusted.example")[0]
            == 403
        )
        assert request("/api/research-archive", post=True)[0] == 403
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)
