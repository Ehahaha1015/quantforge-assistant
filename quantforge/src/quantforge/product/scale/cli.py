"""Host CLI for scale preparation, inspection and one-shot qualification."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .corpus import load_qlib_corpus
from .service import (
    ReplayRuntime,
    ScaleService,
    inspect_benchmark,
    open_benchmark,
    prepare_program,
)
from .vault import HostBenchmarkCapability


def main() -> None:
    parser = argparse.ArgumentParser(description="Bounded research-scale host authority")
    parser.add_argument("--root", required=True, type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--provider-root", type=Path, required=True)
    prepare.add_argument("--universe", type=Path, required=True)
    prepare.add_argument("--declaration", type=Path, required=True)
    prepare.add_argument("--as-of", required=True)
    for name in ("status", "report", "freeze", "benchmark-open", "benchmark-inspect", "pause", "resume", "recover", "replay-start", "replay-stop", "replay-status", "replay-history"):
        sub.add_parser(name)
    reproduce = sub.add_parser("reproduce")
    reproduce.add_argument("run_id")
    replay = sub.add_parser("replay-run")
    replay.add_argument("--steps", type=int, required=True)
    args = parser.parse_args()
    value: Any
    try:
        if args.command == "prepare":
            universe = json.loads(args.universe.read_text(encoding="utf-8"))
            declaration = json.loads(args.declaration.read_text(encoding="utf-8"))
            frames, qualification = load_qlib_corpus(args.provider_root, universe, as_of=datetime.fromisoformat(args.as_of))
            args.root.mkdir(parents=True, exist_ok=True)
            value = prepare_program(args.root, frames, qualification, declaration)
        else:
            service = ScaleService(args.root)
            try:
                if args.command == "benchmark-open":
                    value = open_benchmark(service, HostBenchmarkCapability(service.status()["program_id"]))
                elif args.command == "benchmark-inspect":
                    value = inspect_benchmark(service)
                elif args.command == "reproduce":
                    value = service.reproduce(args.run_id)
                elif args.command == "report":
                    value = service.inspect()
                elif args.command in ("pause", "resume"):
                    value = service.pause(args.command == "pause")
                elif args.command.startswith("replay-"):
                    runtime = ReplayRuntime(service)
                    try:
                        if args.command in ("replay-start", "replay-stop"):
                            value = runtime.set_running(args.command == "replay-start")
                        elif args.command == "replay-run":
                            if not 1 <= args.steps <= 10000:
                                raise ValueError("BOUNDED_REPLAY_STEPS_REQUIRED")
                            events = [runtime.step() for _ in range(args.steps)]
                            value = {"status": runtime.status(), "steps": len(events)}
                        elif args.command == "replay-history":
                            value = runtime.history()
                        else:
                            value = runtime.status()
                    finally:
                        runtime.close()
                else:
                    if args.command == "freeze":
                        value = service.freeze()
                    elif args.command == "recover":
                        value = service.recover()
                    else:
                        value = service.status()
            finally:
                service.close()
        print(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False))
    except (ValueError, OSError, KeyError, TypeError):
        print(json.dumps({"status": "BLOCKED", "reason": "Scale operation denied; inspect retained budgets, bindings and state. No implicit retry or benchmark reopening."}))
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
