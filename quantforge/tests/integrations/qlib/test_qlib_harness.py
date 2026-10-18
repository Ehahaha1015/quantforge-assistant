from __future__ import annotations

import dataclasses
import importlib.util
import json
import math
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from quantforge.experiments import (
    ExperimentRecord,
    ExperimentStore,
    canonical_config_hash,
)
from quantforge.integrations.qlib import (
    BENCHMARK_ID,
    QlibGoldenBenchmarkConfig,
    QlibGoldenBenchmarkResult,
    QlibIntegrationError,
    build_golden_benchmark_spec,
    canonical_spec_json,
    run_golden_benchmark,
    validate_artifact_dir,
    validate_provider_uri,
)

START = datetime(2025, 1, 2, tzinfo=UTC)
WORKSPACE = Path(__file__).resolve().parents[3]


def make_provider(root: Path, *, layout: str = "cn_data") -> Path:
    base = root / layout if layout else root
    for name in ("calendars", "instruments", "features"):
        (base / name).mkdir(parents=True, exist_ok=True)
    (base / "calendars" / "day.txt").write_text("2020-01-02\n", encoding="utf-8")
    (base / "instruments" / "csi300.txt").write_text("SH600000\t2008-01-01\t2020-08-01\n", encoding="utf-8")
    (base / "features" / "sh600000").mkdir(exist_ok=True)
    (base / "features" / "sh600000" / "close.day.bin").write_bytes(b"\x00\x01")
    return root


def write_worker(tmp_path: Path, name: str, body: str) -> Path:
    script = tmp_path / f"{name}.py"
    script.write_text(body, encoding="utf-8")
    return script


SUCCESS_WORKER = """
import json, os, pathlib, sys
spec = json.loads(pathlib.Path(sys.argv[sys.argv.index("--spec") + 1]).read_text())
out = pathlib.Path(sys.argv[sys.argv.index("--result") + 1])
pathlib.Path("cwd_marker.txt").write_text(os.getcwd(), encoding="utf-8")
pathlib.Path("mlflow_env.txt").write_text(os.environ.get("MLFLOW_TRACKING_URI", ""), encoding="utf-8")
for name in ("prediction.pkl", "portfolio_analysis.pkl"):
    pathlib.Path(name).write_text("artifact", encoding="utf-8")
out.write_text(json.dumps({
    "benchmark_id": spec["benchmark_id"],
    "qlib_version": "0.9.7",
    "status": "SUCCESS",
    "evidence": {"signal_analysis": True, "portfolio_analysis": True},
    "metrics": {"z_metric": 0.5, "a_metric": 1.25},
    "artifact_refs": ["portfolio_analysis.pkl", "prediction.pkl"],
}), encoding="utf-8")
"""

FAILED_WORKER = """
import json, pathlib, sys
out = pathlib.Path(sys.argv[sys.argv.index("--result") + 1])
out.write_text(json.dumps({"status": "FAILED", "metrics": {}, "artifact_refs": []}), encoding="utf-8")
sys.exit(3)
"""

FALSE_SUCCESS_WORKER = """
import json, pathlib, sys
out = pathlib.Path(sys.argv[sys.argv.index("--result") + 1])
out.write_text(json.dumps({"benchmark_id": "qlib-alpha158-lightgbm-csi300-v1", "status": "SUCCESS",
    "evidence": {"signal_analysis": True, "portfolio_analysis": True},
    "metrics": {"ic": 1.0}, "artifact_refs": ["prediction.pkl"]}), encoding="utf-8")
sys.exit(1)
"""


def test_optional_dependency_isolation() -> None:
    assert "qlib" not in sys.modules
    import quantforge
    import quantforge.integrations.qlib

    assert "qlib" not in sys.modules
    package = Path(quantforge.integrations.qlib.__file__).parent
    for source in package.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        assert "quantforge.strategies" not in text
        assert "quantforge.backtest" not in text
        assert "from ..strategies" not in text


def test_benchmark_identity_is_fixed() -> None:
    spec = build_golden_benchmark_spec("D:/qlib/providers/cn")
    assert spec.benchmark_id == BENCHMARK_ID == "qlib-alpha158-lightgbm-csi300-v1"
    assert spec.region == "CN"
    assert spec.market == "csi300"
    assert spec.benchmark_index == "SH000300"
    assert spec.handler == "Alpha158"
    assert spec.model == "LGBModel"
    assert spec.train_segment == ("2008-01-01", "2014-12-31")
    assert spec.valid_segment == ("2015-01-01", "2016-12-31")
    assert spec.test_segment == ("2017-01-01", "2020-08-01")
    assert spec.workflow == ("train", "predict", "signal_analysis", "portfolio_analysis")


def test_config_hash_is_deterministic_and_ignores_runtime_paths(tmp_path: Path) -> None:
    first = build_golden_benchmark_spec(tmp_path / "provider-a")
    second = build_golden_benchmark_spec(tmp_path / "provider-b")
    assert first.config_hash == second.config_hash
    assert canonical_spec_json(first) == canonical_spec_json(second)
    assert json.loads(canonical_spec_json(first)) == json.loads(canonical_spec_json(second))
    # mapping order must not matter for the hash
    payload = json.loads(canonical_spec_json(first))
    reordered = dict(reversed(list(payload.items())))
    from hashlib import sha256

    assert sha256(json.dumps(reordered, sort_keys=True, separators=(",", ":")).encode()).hexdigest() == first.config_hash


def test_material_spec_change_changes_hash() -> None:
    spec = build_golden_benchmark_spec("D:/qlib/providers/cn")
    changed = dataclasses.replace(spec, test_segment=("2017-01-01", "2019-12-31"))
    assert changed.config_hash != spec.config_hash
    assert canonical_spec_json(changed) != canonical_spec_json(spec)


def test_provider_validation(tmp_path: Path) -> None:
    with pytest.raises(QlibIntegrationError, match="does not exist"):
        validate_provider_uri(tmp_path / "missing")
    file_path = tmp_path / "provider.txt"
    file_path.write_text("nope", encoding="utf-8")
    with pytest.raises(QlibIntegrationError, match="not a directory"):
        validate_provider_uri(file_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(QlibIntegrationError, match="usable Qlib CN provider"):
        validate_provider_uri(empty)
    partial = tmp_path / "partial"
    (partial / "cn_data" / "calendars").mkdir(parents=True)
    with pytest.raises(QlibIntegrationError, match="usable Qlib CN provider"):
        validate_provider_uri(partial)
    for missing, name in (("calendars", "no_calendar"), ("instruments", "no_instruments"), ("features", "no_features")):
        provider = make_provider(tmp_path / name)
        for item in (provider / "cn_data" / missing).iterdir():
            shutil.rmtree(item) if item.is_dir() else item.unlink()
        with pytest.raises(QlibIntegrationError, match="usable Qlib CN provider"):
            validate_provider_uri(provider)
    nested = validate_provider_uri(make_provider(tmp_path / "nested", layout="cn_data"))
    assert nested.name == "cn_data"
    direct = validate_provider_uri(make_provider(tmp_path / "direct", layout=""))
    assert direct == tmp_path / "direct"


def test_artifact_dir_must_be_explicit_and_outside_the_repository(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="artifact_dir"):
        QlibGoldenBenchmarkConfig(provider_uri="x", artifact_dir="")
    with pytest.raises(QlibIntegrationError, match="outside the QuantForge source tree"):
        validate_artifact_dir(WORKSPACE / "src" / "quantforge" / "integrations" / "tmp-artifacts")
    created = validate_artifact_dir(tmp_path / "artifacts")
    assert created.is_dir()


def test_missing_optional_extra_raises_integration_error(tmp_path: Path) -> None:
    if importlib.util.find_spec("qlib") is not None:
        pytest.skip("qlib extra is installed in this environment")
    provider = make_provider(tmp_path / "provider")
    config = QlibGoldenBenchmarkConfig(
        provider_uri=str(provider), artifact_dir=str(tmp_path / "artifacts")
    )
    with pytest.raises(QlibIntegrationError, match="extra is not installed"):
        run_golden_benchmark(config)


def run_fake(tmp_path: Path, script_body: str, name: str = "worker") -> QlibGoldenBenchmarkResult:
    provider = make_provider(tmp_path / "provider")
    artifacts = tmp_path / "artifacts"
    script = write_worker(tmp_path, name, script_body)
    config = QlibGoldenBenchmarkConfig(
        provider_uri=str(provider), artifact_dir=str(artifacts)
    )
    return run_golden_benchmark(
        config, worker=[sys.executable, str(script)]
    )


def test_successful_worker_evidence_is_normalized(tmp_path: Path) -> None:
    result = run_fake(tmp_path, SUCCESS_WORKER)
    assert result.status == "SUCCESS"
    assert result.benchmark_id == BENCHMARK_ID
    assert result.qlib_version == "0.9.7"
    assert list(result.metrics) == ["a_metric", "z_metric"]
    assert all(math.isfinite(value) for value in result.metrics.values())
    assert len(result.artifact_refs) == 2
    run_dir = next((tmp_path / "artifacts").glob(f"{BENCHMARK_ID}-*"))
    for reference in result.artifact_refs:
        assert Path(reference).is_relative_to(run_dir)
        assert Path(reference).is_file()
    assert (run_dir / "cwd_marker.txt").read_text(encoding="utf-8") == str(run_dir)
    assert (run_dir / "mlflow_env.txt").read_text(encoding="utf-8") == str(run_dir / "mlruns")
    assert (run_dir / "result.json").is_file()
    assert result.config_hash == build_golden_benchmark_spec(str(tmp_path / "provider")).config_hash


def test_failed_external_workflow_is_never_success(tmp_path: Path) -> None:
    failed = run_fake(tmp_path, FAILED_WORKER, name="failed")
    assert failed.status == "FAILED"
    assert failed.metrics == {}
    assert "exit code 3" in failed.detail
    false_success = run_fake(tmp_path, FALSE_SUCCESS_WORKER, name="false_success")
    assert false_success.status == "FAILED"
    assert "process exit is authoritative" in false_success.detail


def test_mismatched_worker_benchmark_id_is_rejected(tmp_path: Path) -> None:
    result = run_fake(
        tmp_path,
        "import json, pathlib, sys\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('--result') + 1])\n"
        "out.write_text(json.dumps({'benchmark_id': 'not-the-pinned-id', 'status': 'SUCCESS',"
        " 'evidence': {'signal_analysis': True, 'portfolio_analysis': True},"
        " 'metrics': {'ic': 1.0}, 'artifact_refs': ['x.pkl']}), encoding='utf-8')\n",
        name="wrong_id",
    )
    assert result.status == "FAILED"
    assert result.benchmark_id == BENCHMARK_ID
    assert "does not match the pinned identity" in result.detail


def test_success_requires_completed_analysis_evidence(tmp_path: Path) -> None:
    incomplete = run_fake(
        tmp_path,
        "import json, pathlib, sys\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('--result') + 1])\n"
        "out.write_text(json.dumps({'status': 'SUCCESS',"
        " 'evidence': {'signal_analysis': True}, 'metrics': {'ic': 1.0},"
        " 'artifact_refs': ['prediction.pkl']}), encoding='utf-8')\n",
        name="incomplete",
    )
    assert incomplete.status == "FAILED"
    assert "completed prediction/signal analysis and portfolio analysis" in incomplete.detail
    empty = run_fake(
        tmp_path,
        "import json, pathlib, sys\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('--result') + 1])\n"
        "out.write_text(json.dumps({'status': 'SUCCESS',"
        " 'evidence': {'signal_analysis': True, 'portfolio_analysis': True},"
        " 'metrics': {}, 'artifact_refs': []}), encoding='utf-8')\n",
        name="empty_success",
    )
    assert empty.status == "FAILED"


def test_ambient_mlflow_destination_does_not_capture_the_run(tmp_path: Path, monkeypatch) -> None:
    ambient = tmp_path / "ambient-mlruns"
    ambient.mkdir()
    monkeypatch.setenv("MLFLOW_TRACKING_URI", str(ambient))
    result = run_fake(tmp_path, SUCCESS_WORKER, name="mlflow")
    assert result.status == "SUCCESS"
    run_dir = next((tmp_path / "artifacts").glob(f"{BENCHMARK_ID}-*"))
    assert (run_dir / "mlflow_env.txt").read_text(encoding="utf-8") == str(run_dir / "mlruns")
    assert not any(ambient.iterdir())


def test_controlled_mlflow_file_store_opt_in_is_forced(tmp_path: Path, monkeypatch) -> None:
    """QLIB-001 controls its own MLflow backend; ambient values cannot disable it."""
    probe = (
        "import json, os, pathlib, sys\n"
        "pathlib.Path('env_probe.json').write_text(json.dumps({\n"
        "  'allow_file_store': os.environ.get('MLFLOW_ALLOW_FILE_STORE'),\n"
        "  'tracking_uri': os.environ.get('MLFLOW_TRACKING_URI'),\n"
        "}), encoding='utf-8')\n"
    )
    # Ambient conflicting value must not become authoritative.
    monkeypatch.setenv("MLFLOW_ALLOW_FILE_STORE", "false")
    run_fake(tmp_path, probe, name="file_store_conflict")
    run_dir = next((tmp_path / "artifacts").glob(f"{BENCHMARK_ID}-*"))
    observed = json.loads((run_dir / "env_probe.json").read_text(encoding="utf-8"))
    assert observed["allow_file_store"] == "true"
    assert Path(observed["tracking_uri"]).is_relative_to(run_dir)
    assert Path(observed["tracking_uri"]).name == "mlruns"

    # Ambient missing value is handled the same way.
    monkeypatch.delenv("MLFLOW_ALLOW_FILE_STORE", raising=False)
    run_fake(tmp_path, probe, name="file_store_missing")
    run_dirs = sorted((tmp_path / "artifacts").glob(f"{BENCHMARK_ID}-*"))
    observed_missing = json.loads(
        (run_dirs[-1] / "env_probe.json").read_text(encoding="utf-8")
    )
    assert observed_missing["allow_file_store"] == "true"
    assert Path(observed_missing["tracking_uri"]).is_relative_to(run_dirs[-1])
    # MLflow state never lands in the repository.
    assert not Path(observed_missing["tracking_uri"]).is_relative_to(WORKSPACE)


def test_exp_manager_config_is_the_pinned_pyqlib_instance_shape(monkeypatch) -> None:
    from quantforge.integrations.qlib import _worker

    monkeypatch.setenv("MLFLOW_TRACKING_URI", "D:/ambient/elsewhere")
    config = _worker.build_exp_manager_config(
        tracking_uri="D:/runs/qlib-alpha158-lightgbm-csi300-v1/mlruns",
        experiment_name=BENCHMARK_ID,
    )
    assert config["class"] == "MLflowExpManager"
    assert config["module_path"] == "qlib.workflow.expm"
    assert config["kwargs"] == {
        "uri": "D:/runs/qlib-alpha158-lightgbm-csi300-v1/mlruns",
        "default_exp_name": BENCHMARK_ID,
    }
    # The obsolete malformed top-level shape must never be emitted.
    assert "artifact_uri" not in config
    assert "uri" not in config
    with pytest.raises(ValueError, match="explicit controlled location"):
        _worker.build_exp_manager_config(tracking_uri="", experiment_name=BENCHMARK_ID)


PINNED_CONFIG_HASH = "a3a6ed673c5a07b558dd3e338c0bdd510fd2c8d35321a205ec4c000f47c9d31a"


def test_worker_runtime_payload_carries_the_canonical_config_hash(tmp_path: Path) -> None:
    provider = make_provider(tmp_path / "provider")
    config = QlibGoldenBenchmarkConfig(
        provider_uri=str(provider), artifact_dir=str(tmp_path / "artifacts")
    )
    script = write_worker(
        tmp_path,
        "payload_probe",
        "import json, pathlib, sys\n"
        "spec = json.loads(pathlib.Path(sys.argv[sys.argv.index('--spec') + 1]).read_text())\n"
        "pathlib.Path('probe.json').write_text(json.dumps({\n"
        "  'config_hash': spec.get('config_hash'),\n"
        "  'provider_uri': spec.get('provider_uri'),\n"
        "  'benchmark_id': spec.get('benchmark_id'),\n"
        "  'keys': sorted(spec),\n"
        "}), encoding='utf-8')\n",
    )
    # The probe writes no result evidence, so this run is FAILED by design.
    assert run_golden_benchmark(config, worker=[sys.executable, str(script)]).status == "FAILED"
    run_dir = next((tmp_path / "artifacts").glob(f"{BENCHMARK_ID}-*"))
    probe = json.loads((run_dir / "probe.json").read_text(encoding="utf-8"))
    spec = build_golden_benchmark_spec(str(provider))
    assert probe["config_hash"] == spec.config_hash == PINNED_CONFIG_HASH
    assert probe["benchmark_id"] == BENCHMARK_ID
    assert probe["provider_uri"] == str(validate_provider_uri(provider))
    # config_hash / provider_uri are runtime fields and must not be part of the
    # semantic material that produced the hash.
    assert "config_hash" not in json.loads(canonical_spec_json(spec))
    assert "provider_uri" not in json.loads(canonical_spec_json(spec))
    # The worker can consume the generated payload without a KeyError.
    assert "config_hash" in probe["keys"] and "provider_uri" in probe["keys"]
    assert run_golden_benchmark(config, worker=[sys.executable, str(script)]).config_hash == spec.config_hash


def test_optional_qlib_accepts_the_exp_manager_config(tmp_path: Path) -> None:
    if importlib.util.find_spec("qlib") is None:
        pytest.skip("qlib extra is not installed in this environment")
    from qlib.utils import init_instance_by_config

    from quantforge.integrations.qlib import _worker

    manager = init_instance_by_config(
        _worker.build_exp_manager_config(
            tracking_uri=str(tmp_path / "mlruns"), experiment_name=BENCHMARK_ID
        )
    )
    assert type(manager).__name__ == "MLflowExpManager"


def test_malformed_or_missing_evidence_is_rejected(tmp_path: Path) -> None:
    missing = run_fake(
        tmp_path,
        "import sys\nsys.exit(0)\n",
        name="missing",
    )
    assert missing.status == "FAILED"
    assert "no result.json" in missing.detail
    malformed = run_fake(
        tmp_path,
        "import pathlib, sys\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('--result') + 1])\n"
        "out.write_text('{not json', encoding='utf-8')\n",
        name="malformed",
    )
    assert malformed.status == "FAILED"
    assert "unusable worker evidence" in malformed.detail
    not_object = run_fake(
        tmp_path,
        "import pathlib, sys\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('--result') + 1])\n"
        "out.write_text('[1, 2]', encoding='utf-8')\n"
        "sys.exit(1)\n",
        name="not_object",
    )
    assert not_object.status == "FAILED"


def test_non_finite_metric_is_rejected(tmp_path: Path) -> None:
    result = run_fake(
        tmp_path,
        "import json, pathlib, sys\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('--result') + 1])\n"
        "out.write_text(json.dumps({'status': 'SUCCESS', 'metrics': {'ic': float('nan')}}), encoding='utf-8')\n",
        name="nan",
    )
    assert result.status == "FAILED"
    assert result.metrics == {}


def test_result_model_rejects_non_finite_and_orders_metrics() -> None:
    with pytest.raises(ValueError, match="must be finite"):
        QlibGoldenBenchmarkResult(
            benchmark_id=BENCHMARK_ID,
            qlib_version="0.9.7",
            config_hash="a" * 64,
            provider_uri="D:/provider",
            status="SUCCESS",
            metrics={"ic": float("inf")},
        )
    result = QlibGoldenBenchmarkResult(
        benchmark_id=BENCHMARK_ID,
        qlib_version="0.9.7",
        config_hash="b" * 64,
        provider_uri="D:/provider",
        status="SUCCESS",
        metrics={"z": 1.0, "a": 2.0},
        artifact_refs=("b.pkl", "a.pkl"),
    )
    assert list(result.metrics) == ["a", "z"]
    assert result.artifact_refs == ("a.pkl", "b.pkl")


def test_experiment_store_records_benchmark_provenance_without_modification(
    tmp_path: Path,
) -> None:
    result = run_fake(tmp_path, SUCCESS_WORKER, name="experiment")
    spec = build_golden_benchmark_spec(str(tmp_path / "provider"))
    payload = json.loads(canonical_spec_json(spec))
    record = ExperimentRecord.create(
        experiment_id="qlib-golden-benchmark",
        git_commit="ac672b15676ac1eb7cbfcca98ac377b697e45ccb",
        dataset_id=BENCHMARK_ID,
        dataset_hash=spec.config_hash,
        backtest_config=payload,
        random_seed=0,
        started_at=START,
        metrics=result.metrics,
        artifact_refs=[
            {"name": f"artifact-{index}", "uri": reference}
            for index, reference in enumerate(result.artifact_refs)
        ],
    )
    store = ExperimentStore(tmp_path / "experiments.sqlite")
    try:
        store.save(record)
    finally:
        store.close()
    reopened = ExperimentStore(tmp_path / "experiments.sqlite")
    try:
        loaded = reopened.get("qlib-golden-benchmark")
    finally:
        reopened.close()
    assert loaded == record
    assert loaded.config_hash == canonical_config_hash(payload)
    assert loaded.dataset_hash == spec.config_hash
    assert loaded.metrics.to_payload() == result.metrics
    assert len(loaded.artifact_refs) == len(result.artifact_refs)
