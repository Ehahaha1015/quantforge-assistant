"""Inspectable local product entrypoint; raw fact mutation is never exposed."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quantforge.experiments import ExperimentStoreError
from quantforge.research.oos import ResearchSealError

from .lockbox import final_evaluate, inspect_final
from .paper import PaperRuntime
from .policy import freeze_candidate
from .runner import ExperimentRunError, ExperimentRunner


def _main() -> None:
    if sys.argv[1:2] == ["canary-check"]:
        from .pilot.readiness_cli import main as readiness_main

        sys.argv.pop(1)
        readiness_main()
        return
    if sys.argv[1:2] == ["scale"]:
        from .scale.cli import main as scale_main

        # Scale owns its additive parser, preserving the v1 command surface.
        sys.argv.pop(1)
        scale_main()
        return
    if sys.argv[1:2] == ["pilot"]:
        from .pilot.cli import main as pilot_main

        # Pilot owns its additive paper-only parser; v1/scale surfaces unchanged.
        sys.argv.pop(1)
        pilot_main()
        return
    parser = argparse.ArgumentParser(
        description="QuantForge deterministic research and paper product"
    )
    parser.add_argument(
        "--root",
        type=Path,
        required=True,
        help="Existing host-owned local artifact directory",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("runs")
    for name in ("inspect", "reproduce"):
        command = sub.add_parser(name)
        command.add_argument("run_id")
    dataset = sub.add_parser("dataset")
    dataset.add_argument("dataset_id")
    freeze = sub.add_parser("freeze")
    freeze.add_argument("run_id")
    freeze.add_argument("--program", required=True)
    for name in (
        "final",
        "final-inspect",
        "paper-start",
        "paper-step",
        "paper-stop",
        "paper-status",
        "paper-history",
        "paper-run",
    ):
        command = sub.add_parser(name)
        command.add_argument("program")
        if name == "paper-run":
            command.add_argument("--steps", type=int, default=0)
            command.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    runner = ExperimentRunner(args.root)
    value: Any
    try:
        if args.command == "runs":
            value = [record.model_dump(mode="json") for record in runner.store.list()]
        elif args.command == "inspect":
            value = runner.inspect(args.run_id)
        elif args.command == "reproduce":
            value = runner.reproduce(args.run_id)
        elif args.command == "dataset":
            snapshot = runner.catalog.verify(args.dataset_id)
            value = {
                "snapshot": snapshot.model_dump(mode="json"),
                "qualification": runner.dataset_qualification(snapshot),
            }
        elif args.command == "freeze":
            value = freeze_candidate(runner, args.run_id, program_id=args.program)
        elif args.command == "final":
            value = final_evaluate(
                runner, program_id=args.program, admitted_at=datetime.now(UTC)
            )
        elif args.command == "final-inspect":
            value = inspect_final(runner, args.program)
        else:
            paper = PaperRuntime(runner, args.program)
            try:
                if args.command == "paper-run":
                    value = paper.run(steps=args.steps, watch=args.watch)
                else:
                    methods = {
                        "paper-start": paper.start,
                        "paper-step": paper.step,
                        "paper-stop": paper.stop,
                        "paper-status": paper.status,
                        "paper-history": paper.history,
                    }
                    value = methods[args.command]()
            finally:
                paper.close()
        print(json.dumps(value, indent=2, ensure_ascii=False))
    finally:
        runner.close()


def main() -> None:
    try:
        _main()
    except (
        ExperimentRunError,
        ExperimentStoreError,
        ResearchSealError,
        ValueError,
        OSError,
    ) as exc:
        if isinstance(exc, ResearchSealError):
            reason = "Final holdout permission denied: inspect the spent seal, retained history and bindings."
        elif isinstance(exc, ExperimentStoreError):
            reason = "Requested experiment is absent or its record is invalid; final evaluation is required before paper start."
        elif isinstance(exc, ExperimentRunError):
            reason = str(exc)
        else:
            reason = "Local input or retained artifact is missing, invalid or unavailable; inspect the requested IDs and provenance."
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "error_type": type(exc).__name__,
                    "reason": reason,
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
