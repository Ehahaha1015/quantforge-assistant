from __future__ import annotations

import importlib.util
import json
import math
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from .spec import GoldenBenchmarkSpec, build_golden_benchmark_spec, canonical_spec_json

_PROVIDER_COMPONENTS = ("calendars", "instruments", "features")
_DEFAULT_TIMEOUT_SECONDS = 3600.0
_MLFLOW_TRACKING_DIRECTORY = "mlruns"


class QlibIntegrationError(RuntimeError):
    """The optional Qlib integration is unusable in the current environment."""


@dataclass(frozen=True)
class QlibGoldenBenchmarkConfig:
    """Caller-supplied runtime inputs for one golden benchmark run."""

    provider_uri: str
    artifact_dir: str

    def __post_init__(self) -> None:
        for name, value in (
            ("provider_uri", self.provider_uri),
            ("artifact_dir", self.artifact_dir),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be an explicit non-empty path")


class QlibGoldenBenchmarkResult(BaseModel):
    """Immutable normalized evidence of one external benchmark attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    benchmark_id: str
    qlib_version: str
    config_hash: str
    provider_uri: str
    status: Literal["SUCCESS", "FAILED"]
    metrics: dict[str, float] = {}
    artifact_refs: tuple[str, ...] = ()
    detail: str = ""

    @field_validator("metrics")
    @classmethod
    def _finite_metrics(cls, value: dict[str, float]) -> dict[str, float]:
        ordered: dict[str, float] = {}
        for key in sorted(value):
            number = float(value[key])
            if not math.isfinite(number):
                raise ValueError(f"benchmark metric {key!r} must be finite")
            ordered[key] = number
        return ordered

    @field_validator("artifact_refs")
    @classmethod
    def _deterministic_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(str(item) for item in value))


def project_root() -> Path:
    """Repository root of the running QuantForge package."""
    return Path(__file__).resolve().parents[4]


def validate_provider_uri(provider_uri: str | Path) -> Path:
    """Validate a caller-supplied Qlib provider without touching its contents.

    The gate requires usable content, not just directory names: a non-empty
    calendar, a non-empty instrument universe and at least one feature shard
    with data, otherwise a superficially named but empty tree would be
    accepted as a provider.
    """
    path = Path(provider_uri).expanduser()
    if not path.exists():
        raise QlibIntegrationError(f"provider_uri does not exist: {path}")
    if not path.is_dir():
        raise QlibIntegrationError(f"provider_uri is not a directory: {path}")
    candidates = [path / "cn_data", path]
    for candidate in candidates:
        if not all((candidate / name).is_dir() for name in _PROVIDER_COMPONENTS):
            continue
        calendars_ok = any(
            item.is_file() and item.stat().st_size > 0
            for item in (candidate / "calendars").iterdir()
        )
        instruments_ok = any(
            item.is_file() and item.stat().st_size > 0
            for item in (candidate / "instruments").iterdir()
        )
        features_ok = any(
            item.is_dir() and any(entry.stat().st_size > 0 for entry in item.iterdir() if entry.is_file())
            for item in (candidate / "features").iterdir()
        )
        if calendars_ok and instruments_ok and features_ok:
            return candidate
    joined = ", ".join(_PROVIDER_COMPONENTS)
    raise QlibIntegrationError(
        f"provider_uri does not look like a usable Qlib CN provider "
        f"(need non-empty {joined}): {path}"
    )


def validate_artifact_dir(artifact_dir: str | Path) -> Path:
    """Validate and create an explicit artifact directory outside the repository."""
    if artifact_dir is None:
        raise QlibIntegrationError("artifact_dir must be supplied explicitly")
    resolved = Path(artifact_dir).expanduser().resolve()
    if resolved.is_relative_to(project_root()):
        raise QlibIntegrationError(
            f"artifact_dir must live outside the QuantForge source tree: {resolved}"
        )
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _load_result(
    result_path: Path,
    *,
    spec: GoldenBenchmarkSpec,
    run_dir: Path,
    detail: str,
) -> QlibGoldenBenchmarkResult:
    if not result_path.is_file():
        return QlibGoldenBenchmarkResult(
            benchmark_id=spec.benchmark_id,
            qlib_version="",
            config_hash=spec.config_hash,
            provider_uri=spec.provider_uri,
            status="FAILED",
            metrics={},
            artifact_refs=(),
            detail=f"{detail}; worker produced no result.json",
        )
    try:
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("worker result evidence must be a JSON object")
        metrics = payload.get("metrics", {})
        artifact_refs = payload.get("artifact_refs", [])
        if not isinstance(metrics, dict) or not isinstance(artifact_refs, list):
            raise TypeError("worker result evidence has malformed metrics or artifacts")
        worker_benchmark_id = payload.get("benchmark_id")
        if worker_benchmark_id is not None and str(worker_benchmark_id) != spec.benchmark_id:
            raise ValueError(
                f"worker benchmark_id {worker_benchmark_id!r} does not match the pinned "
                f"identity {spec.benchmark_id!r}"
            )
        evidence = payload.get("evidence", {})
        if not isinstance(evidence, dict):
            raise TypeError("worker evidence markers must be an object")
        completed = all(
            evidence.get(step) is True for step in ("signal_analysis", "portfolio_analysis")
        )
        refs = tuple(
            str((run_dir / str(item)).resolve())
            if not Path(str(item)).is_absolute()
            else str(item)
            for item in artifact_refs
        )
        claimed = str(payload.get("status", "FAILED")).upper()
        if claimed == "SUCCESS" and not (completed and metrics and refs):
            return QlibGoldenBenchmarkResult(
                benchmark_id=spec.benchmark_id,
                qlib_version=str(payload.get("qlib_version", "")),
                config_hash=spec.config_hash,
                provider_uri=spec.provider_uri,
                status="FAILED",
                metrics={},
                artifact_refs=refs,
                detail=(
                    f"{detail}; SUCCESS requires completed prediction/signal analysis and "
                    "portfolio analysis evidence"
                ),
            )
        return QlibGoldenBenchmarkResult(
            benchmark_id=spec.benchmark_id,
            qlib_version=str(payload.get("qlib_version", "")),
            config_hash=spec.config_hash,
            provider_uri=spec.provider_uri,
            status="SUCCESS" if claimed == "SUCCESS" else "FAILED",
            metrics={str(key): float(value) for key, value in metrics.items()},
            artifact_refs=refs,
            detail=str(payload.get("detail", detail)),
        )
    except (OSError, TypeError, ValueError) as exc:
        return QlibGoldenBenchmarkResult(
            benchmark_id=spec.benchmark_id,
            qlib_version="",
            config_hash=spec.config_hash,
            provider_uri=spec.provider_uri,
            status="FAILED",
            metrics={},
            artifact_refs=(),
            detail=f"{detail}; unusable worker evidence: {type(exc).__name__}: {exc}",
        )


def run_golden_benchmark(
    config: QlibGoldenBenchmarkConfig,
    *,
    worker: list[str] | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> QlibGoldenBenchmarkResult:
    """Run the fixed Alpha158 + LightGBM golden workflow via an isolated process.

    The workflow always executes in a subprocess with structured argv, a
    working directory inside the caller-supplied ``artifact_dir``, and no
    shell, so Qlib/MLflow global state and its artifacts cannot leak into the
    repository.  A missing optional extra raises the integration-specific
    error instead of a bare ``ImportError``.
    """
    if not isinstance(config, QlibGoldenBenchmarkConfig):
        raise TypeError("config must be a QlibGoldenBenchmarkConfig")
    spec = build_golden_benchmark_spec(config.provider_uri)
    provider_root = validate_provider_uri(config.provider_uri)
    artifact_root = validate_artifact_dir(config.artifact_dir)
    if worker is None and importlib.util.find_spec("qlib") is None:
        raise QlibIntegrationError(
            "the optional 'qlib' extra is not installed; run with `uv run --extra qlib ...`"
        )

    run_dir = artifact_root / f"{spec.benchmark_id}-{spec.config_hash[:12]}"
    run_dir.mkdir(parents=True, exist_ok=True)
    tracking_dir = run_dir / _MLFLOW_TRACKING_DIRECTORY
    tracking_dir.mkdir(parents=True, exist_ok=True)
    spec_path = run_dir / "benchmark_spec.json"
    result_path = run_dir / "result.json"
    spec_path.write_text(canonical_spec_json(spec), encoding="utf-8")
    if result_path.exists():
        result_path.unlink()
    payload = json.loads(spec_path.read_text(encoding="utf-8"))
    payload["provider_uri"] = str(provider_root)
    # Runtime/provenance handoff only: config_hash is computed by the parent
    # from the semantic payload and never feeds back into that computation.
    payload["config_hash"] = spec.config_hash
    spec_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")

    command = list(worker) if worker is not None else [sys.executable, "-m", "quantforge.integrations.qlib._worker"]
    argv = [*command, "--spec", str(spec_path), "--result", str(result_path)]
    environment = os.environ.copy()
    # Benchmark tracking must stay inside the caller-controlled run directory;
    # an ambient MLflow destination must not capture benchmark state.
    environment["MLFLOW_TRACKING_URI"] = str(tracking_dir)
    # pyqlib 0.9.7's MLflowExpManager uses a file-store URI; newer MLflow
    # refuses that backend unless it is explicitly opted into.  The opt-in is
    # scoped to this controlled subprocess and never touches the caller's
    # environment.
    environment["MLFLOW_ALLOW_FILE_STORE"] = "true"
    try:
        completed = subprocess.run(
            argv,
            cwd=str(run_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=timeout_seconds,
            check=False,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        (run_dir / "worker.stderr.log").write_text(str(exc), encoding="utf-8")
        return QlibGoldenBenchmarkResult(
            benchmark_id=spec.benchmark_id,
            qlib_version="",
            config_hash=spec.config_hash,
            provider_uri=spec.provider_uri,
            status="FAILED",
            metrics={},
            artifact_refs=(str(run_dir / "worker.stderr.log"),),
            detail=f"benchmark timed out after {timeout_seconds} seconds",
        )
    (run_dir / "worker.stdout.log").write_text(completed.stdout or "", encoding="utf-8")
    (run_dir / "worker.stderr.log").write_text(completed.stderr or "", encoding="utf-8")
    detail = f"worker exit code {completed.returncode}"
    result = _load_result(
        result_path, spec=spec, run_dir=run_dir, detail=detail
    )
    if completed.returncode != 0:
        # The process boundary is authoritative: a non-zero exit can never
        # become SUCCESS evidence, whatever the payload claims.
        return result.model_copy(
            update={"status": "FAILED", "detail": f"{detail}; process exit is authoritative"}
        )
    return result
