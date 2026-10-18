"""Research CLI using the same protected bindings and lifetime budgets as the UI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from qf_lab.library import library
from qf_lab.report import report

from .bindings import BindingRegistry
from .research_room import RoomService
from .storage import PlatformError


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        required=True,
        help="Existing platform root with protected bindings",
    )
    parser.add_argument("action", choices=("run", "status", "memory", "report"))
    parser.add_argument("--program")
    parser.add_argument("--dataset")
    parser.add_argument("--goal")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--budget-calls", type=int, default=6)
    parser.add_argument("--budget-tokens", type=int, default=250000)
    parser.add_argument("--budget-cost", default="0.25")
    parser.add_argument("--budget-wall", type=int, default=600)
    args = parser.parse_args()
    if not (args.root / "bindings.sqlite").is_file():
        parser.error("an existing protected binding registry is required")
    service = RoomService(args.root, BindingRegistry(args.root))
    if args.program:
        service.select(args.program)
    if args.action == "run":
        if not args.dataset or not args.goal:
            parser.error("--dataset and --goal are required")
        service.start(
            {
                "mode": "HISTORICAL",
                "dataset_id": args.dataset,
                "goal": args.goal,
                "rounds": args.rounds,
                "calls": args.budget_calls,
                "tokens": args.budget_tokens,
                "cost": args.budget_cost,
                "wall_seconds": args.budget_wall,
            }
        )
        assert service.worker is not None
        service.worker.join()
        result = service.snapshot()
    elif args.action == "memory":
        result = library(service.rooms)
    elif args.action == "report":
        if service.current is None:
            parser.error("no declared program")
        result = report(service.current)
    else:
        result = service.snapshot()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if args.action == "run" and result["status"] != "COMPLETE":
        raise SystemExit(2)


def main() -> None:
    from qf_lab.gateway import GatewayError

    try:
        _main()
    except (GatewayError, PlatformError, ValueError, OSError):
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "reason": "RESEARCH_PREFLIGHT_FAILED_INSPECT_RETAINED_HISTORY",
                }
            )
        )
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
