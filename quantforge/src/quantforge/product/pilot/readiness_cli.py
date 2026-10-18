"""Read-only manual canary preflight; non-ready reports exit with code 2."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..storage import local_root
from .broker import broker_status
from .readiness import CanaryRequest, check_canary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only 100 CNY manual canary preflight"
    )
    parser.add_argument(
        "--root", required=True, type=Path, help="Existing declared broker ledger root"
    )
    parser.add_argument(
        "--input", required=True, type=Path, help="Local operator observations JSON"
    )
    args = parser.parse_args()
    report: dict[str, Any]
    try:
        local_root(args.input.parent)
        if (
            args.input.is_symlink()
            or not args.input.is_file()
            or args.input.stat().st_size > 65536
        ):
            raise ValueError("bounded regular local input required")
        request = CanaryRequest.model_validate_json(args.input.read_bytes())
        report = check_canary(request, broker_status(args.root), now=datetime.now(UTC))
    except ValidationError as exc:
        report = {
            "status": "BLOCKED",
            "blocks": ["INVALID_INPUT"],
            "invalid_fields": [
                ".".join(map(str, item["loc"])) for item in exc.errors()
            ],
            "real_transport": "LOCKED",
            "no_order_sent": True,
        }
    except (
        ValueError,
        ArithmeticError,
        OSError,
        KeyError,
        TypeError,
        sqlite3.DatabaseError,
    ):
        report = {
            "status": "BLOCKED",
            "blocks": ["INPUT_OR_RETAINED_LEDGER_UNAVAILABLE"],
            "real_transport": "LOCKED",
            "no_order_sent": True,
        }
    print(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False))
    if report["status"] != "READY_FOR_MANUAL_REVIEW":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
