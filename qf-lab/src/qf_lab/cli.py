from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .controller import RoundController, inspect_history, list_programs
from .gateway import (
    FAMILIES,
    DeepSeekProvider,
    FakeProvider,
    GatewayError,
    OpenAIProvider,
    Provider,
)


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="QuantForge isolated AI research laboratory"
    )
    parser.add_argument("--core-root", type=Path, required=True)
    parser.add_argument("--lab-root", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("programs", help="Inspect persisted research program progress")
    loop = sub.add_parser("loop", help="Bounded multi-role research room")
    loop.add_argument(
        "action", choices=("run", "resume", "pause", "stop", "status", "report")
    )
    loop.add_argument("--program", required=True)
    loop.add_argument("--goal")
    loop.add_argument("--dataset")
    loop.add_argument("--rounds", type=int, default=3)
    loop.add_argument("--budget-calls", type=int, default=32)
    loop.add_argument("--budget-tokens", type=int, default=250000)
    loop.add_argument("--budget-cost", default="1")
    loop.add_argument("--budget-wall", type=int, default=600)
    loop.add_argument("--memory-root", type=Path)
    loop.add_argument("--provider", choices=("fake", "configured"), default="fake")
    loop.add_argument("--routing", type=Path)
    memory = sub.add_parser("memory", help="Rebuild read-only intelligence memory")
    memory.add_argument("--program", required=True)
    memory.add_argument(
        "view",
        choices=("all", "hypotheses", "failures", "decisions", "strategies", "regimes"),
        default="all",
        nargs="?",
    )
    sub.add_parser(
        "data", help="Download and qualify the official public IBM historical demo"
    )
    research = sub.add_parser("research")
    research.add_argument("goal")
    research.add_argument("--dataset", required=True)
    research.add_argument("--program", required=True)
    research.add_argument(
        "--legacy",
        action="store_true",
        help="Explicit compatibility mode for retained legacy programs; new paid work uses loop or qf-research.",
    )
    research.add_argument(
        "--family",
        choices=FAMILIES,
        action="append",
        help="Allowed signal family; repeat to require a cross-family second round. Omit for legacy SMA-v1.",
    )
    research.add_argument(
        "--provider", choices=("deepseek", "fake"), default="deepseek"
    )
    research.add_argument(
        "--critic-provider",
        choices=("deepseek", "openai", "fake"),
        help="Independent critic configuration; omitted reuses the primary provider.",
    )
    research.add_argument(
        "--critic-model", help="Explicit model for the configured critic provider."
    )
    inspect = sub.add_parser("inspect")
    inspect.add_argument("program")
    scale = sub.add_parser(
        "scale", help="Operate a precommitted research-scale campaign"
    )
    scale.add_argument(
        "action", choices=("run", "resume", "pause", "status", "history")
    )
    scale.add_argument("--steps", type=int, default=1)
    scale.add_argument("--freeze-when-exhausted", action="store_true")
    scale.add_argument("--provider", choices=("deepseek", "fake"), default="deepseek")
    scale.add_argument(
        "--legacy",
        action="store_true",
        help="Explicit retained scale-v1 compatibility mode",
    )
    args = parser.parse_args()
    value: Any
    if args.command in {"loop", "memory"}:
        from .room_cli import room_cli

        value = room_cli(args)
    elif args.command == "scale":
        from quantforge.product.scale.service import ScaleService

        from .scale import ScaleController, inspect_scale_history

        if args.action in ("run", "resume"):
            if args.provider != "fake" and not args.legacy:
                raise GatewayError("LEGACY_PAID_ENTRY_REQUIRES_EXPLICIT_MODE_USE_ROOM")
            provider = FakeProvider() if args.provider == "fake" else DeepSeekProvider()
            value = ScaleController(args.core_root, args.lab_root, provider).run(
                steps=args.steps,
                freeze_when_exhausted=args.freeze_when_exhausted,
                resume=args.action == "resume",
            )
        else:
            core = ScaleService(args.core_root)
            try:
                if args.action == "pause":
                    value = core.pause(True)
                elif args.action == "history":
                    value = inspect_scale_history(
                        args.lab_root, core.status()["manifest_id"]
                    )
                else:
                    value = core.status()
            finally:
                core.close()
    elif args.command == "data":
        from quantforge.historical.alphavantage import download_snapshot

        from .historical import fetch_public_demo

        args.core_root.mkdir(parents=True, exist_ok=True)
        value = download_snapshot(
            args.core_root, retrieved_at=datetime.now(UTC), fetcher=fetch_public_demo
        ).model_dump(mode="json")
    elif args.command == "research":
        if args.provider != "fake" and not args.legacy:
            raise GatewayError("LEGACY_PAID_ENTRY_REQUIRES_EXPLICIT_MODE_USE_ROOM")
        provider = FakeProvider() if args.provider == "fake" else DeepSeekProvider()
        critic: Provider | None = None
        if args.critic_model and not args.critic_provider:
            raise GatewayError("CRITIC_PROVIDER_REQUIRED")
        if args.critic_provider == "openai":
            critic = OpenAIProvider(args.critic_model or "gpt-4.1-mini")
        elif args.critic_provider == "deepseek":
            critic = DeepSeekProvider(args.critic_model or "deepseek-flash")
        elif args.critic_provider == "fake":
            if args.critic_model:
                raise GatewayError("FAKE_CRITIC_MODEL_FIXED")
            critic = FakeProvider()
        value = RoundController(
            args.core_root,
            args.lab_root,
            provider,
            critic_provider=critic,
        ).run(
            args.goal,
            dataset_id=args.dataset,
            program_id=args.program,
            allowed_families=tuple(args.family) if args.family else None,
        )
    elif args.command == "programs":
        value = list_programs(args.lab_root)
    else:
        value = inspect_history(args.lab_root, args.program)
    print(json.dumps(value, indent=2, ensure_ascii=False))


def main() -> None:
    try:
        _main()
    except (GatewayError, ValueError, OSError, sqlite3.Error) as exc:
        reason = (
            str(exc)
            if isinstance(exc, GatewayError)
            else "Research admission or local program/artifact precondition failed; inspect Core bindings and Lab failure history."
        )
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
