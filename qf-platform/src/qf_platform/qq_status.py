"""Fail-closed transport liveness read; contains no tokens or QQ identity."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path


def connected(root: Path) -> bool:
    directory = root / "qq-bridge"
    path = directory / "connection.json"
    try:
        if directory.is_symlink() or path.is_symlink() or path.stat().st_size > 4096:
            return False
        value = json.loads(path.read_bytes())
        observed = datetime.fromisoformat(value["observed_at"])
        return bool(
            value.get("schema_version") == "qq-connection-v1"
            and value.get("connected") is True
            and observed.tzinfo is not None
            and 0 <= (datetime.now(UTC) - observed).total_seconds() < 45
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False
