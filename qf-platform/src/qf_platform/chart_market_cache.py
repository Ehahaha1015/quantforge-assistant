"""Small query cache over immutable, hash-verified market-response artifacts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quantforge.product.storage import local_root


def cached_fetch(
    root: Path,
    query: dict[str, Any],
    fetch: Callable[[], tuple[bytes, str]],
) -> tuple[bytes, str, str, bool]:
    directory = local_root(root)
    key = hashlib.sha256(json.dumps(query, sort_keys=True).encode()).hexdigest()
    index = directory / ("query-" + key + ".json")
    if index.is_symlink():
        raise ValueError("行情缓存索引被重定向。")
    now = datetime.now(UTC)
    if index.exists():
        entry = json.loads(index.read_bytes())
        if entry["query"] != query:
            raise ValueError("行情缓存查询不一致。")
        if (
            0
            <= (now - datetime.fromisoformat(entry["retrieved_at"])).total_seconds()
            <= 300
        ):
            checksum = entry["sha256"]
            if (
                not isinstance(checksum, str)
                or len(checksum) != 64
                or any(c not in "0123456789abcdef" for c in checksum)
            ):
                raise ValueError("行情缓存摘要不合法。")
            path = directory / (checksum + ".json")
            if path.is_symlink():
                raise ValueError("行情缓存原响应被重定向。")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != checksum:
                raise ValueError("行情缓存原响应摘要不一致。")
            return raw, entry["url"], entry["retrieved_at"], True
    raw, url = fetch()
    checksum = hashlib.sha256(raw).hexdigest()
    artifact = directory / (checksum + ".json")
    if artifact.is_symlink():
        raise ValueError("行情归档路径异常。")
    if artifact.exists():
        if artifact.read_bytes() != raw:
            raise ValueError("行情归档摘要冲突。")
    else:
        with artifact.open("xb") as stream:
            stream.write(raw)
    entry = {
        "query": query,
        "sha256": checksum,
        "url": url,
        "retrieved_at": now.isoformat(),
    }
    temporary = index.with_suffix(".pending")
    if temporary.is_symlink():
        raise ValueError("行情缓存写入路径被重定向。")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(entry, stream)
    temporary.replace(index)
    return raw, url, now.isoformat(), False
