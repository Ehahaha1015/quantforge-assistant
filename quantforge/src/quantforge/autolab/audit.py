from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SECRET_PATTERNS = (
    (
        re.compile(r"(?i)((?:authorization|proxy-authorization)\s*[:=]\s*)[^\r\n]+"),
        r"\1[REDACTED]",
    ),
    (re.compile(r"(?i)(bearer\s+)[^\s,;]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(basic\s+)[^\s,;]+"), r"\1[REDACTED]"),
    (
        re.compile(r"(?i)((?:api[_-]?key|token|password|secret)\s*[:=]\s*)[^\s,;]+"),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)([\"']?(?:api[_-]?key|token|password|secret|credential)[\"']?\s*:\s*[\"']?)[^\"',}\s]+"
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)([A-Z0-9_-]*(?:secret|token|password|credential|api[_-]?key)[A-Z0-9_-]*\s*[:=]\s*)[^\s,;]+"
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)\b(?:AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9]{12,}|xox[baprs]-[A-Za-z0-9-]{12,})\b"
        ),
        "[REDACTED]",
    ),
)
_SECRET_KEYS = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "token",
        "access_token",
        "password",
        "secret",
        "credential",
        "credentials",
        "cookie",
    }
)


def _is_secret_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return any(
        token in normalized
        for token in (
            "authorization",
            "apikey",
            "token",
            "accesstoken",
            "password",
            "secret",
            "credential",
            "cookie",
        )
    )


def redact_text(value: str, limit: int = 4096) -> str:
    """Bound and redact untrusted process output before durable audit storage."""
    redacted = value
    for pattern, replacement in _SECRET_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted[:limit]


def _jsonable(value: Any) -> Any:
    if hasattr(value, "value"):
        return value.value
    if hasattr(value, "to_dict"):
        return _jsonable(value.to_dict())
    if hasattr(value, "__dataclass_fields__"):
        return {k: _jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]"
            if str(key).lower() in _SECRET_KEYS or _is_secret_key(str(key))
            else _jsonable(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


class AuditStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def _target(self, path: str | Path) -> Path:
        target = Path(path).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"audit path escapes configured root: {path!r}") from exc
        return target

    def create_run(self, task_id: str, run_id: str, payload: dict[str, Any]) -> Path:
        for component in (task_id, run_id):
            if not _SAFE_COMPONENT.fullmatch(component):
                raise ValueError(f"unsafe audit path component: {component!r}")
        directory = self._target(self.root / task_id / run_id)
        directory.mkdir(parents=True, exist_ok=True)
        self.write(
            directory / "run.json",
            {
                "task_id": task_id,
                "created_at": datetime.now(UTC).isoformat(),
                **payload,
            },
        )
        return directory

    def write(self, path: str | Path, value: Any) -> None:
        target = self._target(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(_jsonable(value), indent=2, sort_keys=True), encoding="utf-8"
        )

    def write_fix_request(
        self,
        directory: str | Path,
        task_id: str,
        review_commit: str,
        task_contract: str,
        confirmed_blockers: Any,
        deferred_findings: Any,
        required_ci: Any,
    ) -> Path:
        path = Path(directory) / "fix_request.json"
        self.write(
            path,
            {
                "task_id": task_id,
                "review_commit": review_commit,
                "task_contract": task_contract,
                "confirmed_blockers": confirmed_blockers,
                "deferred_findings": deferred_findings,
                "required_ci": required_ci,
            },
        )
        return path
