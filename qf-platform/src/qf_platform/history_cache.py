"""Bounded code/data-bound history checkpoints, never model-request recovery."""

from __future__ import annotations

import gzip
import hashlib
import importlib.metadata
import json
import os
import platform
import time
import uuid
from pathlib import Path
from typing import Any

import polars as pl
from quantforge.data.bars import logical_content_hash
from quantforge.product.context import source_identity
from quantforge.product.storage import local_root, writer_lock
from quantforge.strategies.daily_validation import build_history

from .storage import canonical, digest

MAX_RAW = 64 * 1024 * 1024


def runtime_identity() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        **{
            name: importlib.metadata.version(name)
            for name in ("polars", "pydantic", "duckdb")
        },
    }


def cached_history(
    daily: pl.DataFrame,
    weekly: pl.DataFrame | None,
    benchmark: pl.DataFrame | None,
    root: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    started = time.perf_counter()
    directory = root / "history-cache"
    directory.mkdir(exist_ok=True)
    directory = local_root(directory)
    binding = {
        "runtime": runtime_identity(),
        "schema": "history-checkpoint-v1",
        "core": source_identity(),
        "cache_code": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "daily": logical_content_hash(daily),
        "weekly": logical_content_hash(weekly) if weekly is not None else None,
        "benchmark": logical_content_hash(benchmark) if benchmark is not None else None,
    }
    path = directory / (digest(binding) + ".json.gz")
    with writer_lock(directory):
        if path.is_symlink():
            raise ValueError("History cache redirected")
        hit = path.exists()
        if hit:
            try:
                with gzip.open(path, "rb") as stream:
                    raw = stream.read(MAX_RAW + 1)
            except EOFError as error:
                raise ValueError("History cache is incomplete") from error
            if len(raw) > MAX_RAW:
                raise ValueError("History cache exceeds bound")
            saved = json.loads(raw)
            if (
                saved["binding"] != binding
                or digest(saved["history"]) != saved["sha256"]
            ):
                raise ValueError("History cache binding or digest differs")
            result = saved["history"]
        else:
            result = build_history(daily, weekly, benchmark)
            raw = canonical(
                {"binding": binding, "history": result, "sha256": digest(result)}
            ).encode()
            if (
                len(raw) > MAX_RAW
                or sum(p.stat().st_size for p in directory.glob("*.gz"))
                > 128 * 1024 * 1024
            ):
                raise ValueError("History cache storage bound reached")
            pending = directory / (uuid.uuid4().hex + ".pending")
            with (
                pending.open("xb") as target,
                gzip.GzipFile(fileobj=target, mode="wb", mtime=0) as stream,
            ):
                stream.write(raw)
            os.replace(pending, path)
    return result, {
        "cache_hit": hit,
        "elapsed_seconds": round(time.perf_counter() - started, 6),
        "binding_sha256": digest(binding),
        "scope": "DETERMINISTIC_SYMBOL_HISTORY_ONLY",
    }
