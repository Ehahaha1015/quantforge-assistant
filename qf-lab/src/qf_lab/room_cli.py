"""Research Room CLI, with environment references rather than persisted keys."""

from __future__ import annotations

import json
import os
import re
from decimal import Decimal
from typing import Any

from .gateway import GatewayError
from .protocols import ProtocolProvider
from .room import ResearchRoom
from .room_gateway import FakeRoomProvider, StructuredProvider


def room_cli(args: Any) -> dict[str, Any]:
    program = args.program
    if (
        not program
        or len(program) > 30
        or not program.isascii()
        or any(not (c.isalnum() or c in "_-") for c in program)
    ):
        raise GatewayError("ROOM_PROGRAM_INVALID")
    root = args.lab_root / program
    if args.command == "memory" or args.action in {"status", "pause", "stop", "report"}:
        if not root.is_dir():
            raise GatewayError("ROOM_NOT_DECLARED")
        routes: dict[str, list[StructuredProvider]] = {
            r: [FakeRoomProvider(r)]
            for r in ("researcher", "critic", "director", "validator")
        }
    elif args.provider == "fake":
        routes = {
            r: [FakeRoomProvider(r)]
            for r in ("researcher", "critic", "director", "validator")
        }
    else:
        if args.routing is None:
            raise GatewayError("ROOM_ROUTING_FILE_REQUIRED")
        try:
            config = json.loads(args.routing.read_text(encoding="utf-8"))
            if (
                not isinstance(config, dict)
                or set(config) != {"researcher", "critic", "director", "validator"}
                or any(
                    not isinstance(pool, list) or not pool for pool in config.values()
                )
            ):
                raise ValueError
        except (ValueError, TypeError):
            raise GatewayError("ROOM_ROUTING_INVALID") from None
        routes = {}
        for role, pool in config.items():
            routes[role] = []
            for item in pool:
                if (
                    not isinstance(item, dict)
                    or set(item)
                    != {
                        "profile_id",
                        "protocol",
                        "base_url",
                        "model",
                        "key_env",
                        "input_price",
                        "output_price",
                    }
                    or not isinstance(item["key_env"], str)
                    or not re.fullmatch(
                        r"[A-Z][A-Z0-9_]*_(?:KEY|TOKEN|SECRET)", item["key_env"]
                    )
                ):
                    raise GatewayError("ROOM_ROUTING_INVALID")

                def secret(reference: str = item["key_env"]) -> dict[str, Any]:
                    key = os.environ.get(reference)
                    if not key:
                        raise GatewayError("MODEL_CREDENTIAL_REQUIRED")
                    return {"api_key": key}

                routes[role].append(
                    ProtocolProvider(
                        item["profile_id"],
                        item["protocol"],
                        item["base_url"],
                        item["model"],
                        secret=secret,
                        input_price=Decimal(item["input_price"]),
                        output_price=Decimal(item["output_price"]),
                    )
                )
    if not root.exists():
        if args.command == "memory" or args.action != "run":
            raise GatewayError("ROOM_NOT_DECLARED")
        root.mkdir(parents=True)
    app = ResearchRoom(
        root, args.core_root, routes, memory_root=getattr(args, "memory_root", None)
    )
    if args.command == "memory":
        memory = app.memory()
        return (
            memory
            if args.view == "all"
            else {
                "memory_id": memory["memory_id"],
                "view": args.view,
                "items": memory["INTELLIGENCE_MEMORY"][args.view],
            }
        )
    if args.action == "run":
        if app.snapshot()["status"] == "UNDECLARED":
            if args.dataset is None or args.goal is None:
                raise GatewayError("ROOM_GOAL_AND_DATASET_REQUIRED")
            app.declare(
                program_id=program,
                goal=args.goal,
                dataset_id=args.dataset,
                rounds=args.rounds,
                calls=args.budget_calls,
                tokens=args.budget_tokens,
                cost=Decimal(args.budget_cost),
                wall_seconds=args.budget_wall,
            )
        return app.run()
    if args.action == "resume":
        app.control("resume")
        return app.run()
    if args.action in {"pause", "stop"}:
        return app.control(args.action)
    if args.action == "report":
        from .report import report

        return report(app)
    return app.snapshot()
