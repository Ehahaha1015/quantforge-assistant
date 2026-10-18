"""Subprocess entry point for the pinned Qlib Alpha158 + LightGBM workflow.

Qlib is imported lazily inside this worker so that importing QuantForge core
never initializes Qlib, and so the parent process can report a clear
integration error when the optional extra is missing.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
from typing import Any


def build_exp_manager_config(
    *, tracking_uri: str, experiment_name: str
) -> dict[str, Any]:
    """Pinned pyqlib 0.9.7 instance configuration for the controlled MLflow store.

    pyqlib 0.9.7 resolves ``exp_manager`` through ``init_instance_by_config``,
    so the value must be a Qlib instance config (``class``/``module_path``/
    ``kwargs``); a bare ``{"uri": ..., "artifact_uri": ...}`` mapping fails with
    ``KeyError: 'func'``.  The tracking URI is always the explicitly controlled
    benchmark location, never an ambient MLflow destination.
    """
    if not tracking_uri:
        raise ValueError("tracking_uri must be an explicit controlled location")
    return {
        "class": "MLflowExpManager",
        "module_path": "qlib.workflow.expm",
        "kwargs": {"uri": tracking_uri, "default_exp_name": experiment_name},
    }


def _workflow_config(spec: dict[str, Any]) -> dict[str, Any]:
    data_handler_config = {
        "start_time": spec["train_segment"][0],
        "end_time": spec["test_segment"][1],
        "fit_start_time": spec["train_segment"][0],
        "fit_end_time": spec["train_segment"][1],
        "instruments": spec["market"],
    }
    return {
        "data_handler_config": data_handler_config,
        "dataset": {
            "class": "DatasetH",
            "module_path": "qlib.data.dataset",
            "kwargs": {
                "handler": {
                    "class": spec["handler"],
                    "module_path": "qlib.contrib.data.handler",
                    "kwargs": data_handler_config,
                },
                "segments": {
                    "train": spec["train_segment"],
                    "valid": spec["valid_segment"],
                    "test": spec["test_segment"],
                },
            },
        },
        "model": {
            "class": spec["model"],
            "module_path": "qlib.contrib.model.gbdt",
            "kwargs": {"loss": "mse", "colsample_bytree": 0.8879, "learning_rate": 0.0421},
        },
    }


def _run(spec: dict[str, Any]) -> dict[str, Any]:
    import qlib  # type: ignore[import-not-found,import-untyped]
    from qlib.constant import REG_CN  # type: ignore[import-not-found,import-untyped]
    from qlib.utils import (  # type: ignore[import-not-found,import-untyped]
        init_instance_by_config,
    )
    from qlib.workflow import R  # type: ignore[import-not-found,import-untyped]
    from qlib.workflow.record_temp import (  # type: ignore[import-not-found,import-untyped]
        PortAnaRecord,
        SigAnaRecord,
        SignalRecord,
    )

    qlib.init(
        provider_uri=spec["provider_uri"],
        region=REG_CN,
        exp_manager=build_exp_manager_config(
            tracking_uri=os.environ.get("MLFLOW_TRACKING_URI", ""),
            experiment_name=str(spec["benchmark_id"]),
        ),
    )
    config = _workflow_config(spec)
    with R.start(experiment_name=spec["benchmark_id"], recorder_name=spec["config_hash"][:12]):
        dataset = init_instance_by_config(config["dataset"])
        model = init_instance_by_config(config["model"])
        model.fit(dataset)
        recorder = R.get_recorder()
        SignalRecord(model, dataset, recorder).generate()
        SigAnaRecord(recorder).generate()
        PortAnaRecord(
            recorder,
            config={
                "strategy": {
                    "class": "TopkDropoutStrategy",
                    "module_path": "qlib.contrib.strategy",
                    "kwargs": {"signal": "<PRED>", "topk": 50, "n_drop": 5},
                },
                "backtest": {
                    "start_time": spec["test_segment"][0],
                    "end_time": spec["test_segment"][1],
                    "account": 100000000,
                    "benchmark": spec["benchmark_index"],
                },
            },
        ).generate()
        metrics: dict[str, float] = {}
        for key, value in recorder.list_metrics().items():
            number = float(value)
            if math.isfinite(number):
                metrics[str(key)] = number
        artifact_root = Path(recorder.get_artifact_uri())
        artifacts = [str((artifact_root / str(item)).resolve()) for item in recorder.list_artifacts()]
    return {
        "benchmark_id": spec["benchmark_id"],
        "qlib_version": str(getattr(qlib, "__version__", "")),
        "status": "SUCCESS",
        "evidence": {"signal_analysis": True, "portfolio_analysis": True},
        "metrics": metrics,
        "artifact_refs": artifacts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spec", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args(argv)
    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    result_path = Path(args.result)
    try:
        payload = _run(spec)
    except Exception as exc:  # noqa: BLE001 - the worker reports any failure as evidence
        payload = {
            "benchmark_id": str(spec.get("benchmark_id", "")),
            "qlib_version": "",
            "status": "FAILED",
            "metrics": {},
            "artifact_refs": [],
            "detail": f"{type(exc).__name__}: {exc}",
        }
        result_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        return 1
    result_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
