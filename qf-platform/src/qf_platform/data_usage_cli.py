"""Record already-exposed long-term research snapshots before future admissions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import polars as pl
from quantforge.product.data_usage import DataUsageRegistry

from .dataset_use import REGISTRY_ROOT, register_research_use, window_of
from .storage import digest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    args = parser.parse_args()
    if args.inventory.is_symlink():
        parser.error("regular inventory required")
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    rows = []
    for symbol, quality in inventory["assets"].items():
        frame = pl.read_parquet(args.inventory.parent / (symbol + "-bars.parquet"))
        if window_of(frame).content_hash != quality["content_hash"]:
            raise ValueError("quality inventory and bars disagree")
        receipt = register_research_use(
            frame,
            program="prior-research-20261013",
            reference_hash=digest(inventory),
            purpose="DEVELOPMENT",
        )
        rows.append({"symbol": symbol, "window": window_of(frame).to_json(), **receipt})
    registry = DataUsageRegistry(REGISTRY_ROOT)
    denials = []
    for row in rows:
        frame = pl.read_parquet(
            args.inventory.parent / (row["symbol"] + "-bars.parquet")
        )
        try:
            registry.reserve_final(
                window_of(frame),
                program="forbidden-relabel",
                protocol_hash=digest({"fixed": True}),
            )
        except ValueError as error:
            denials.append({"symbol": row["symbol"], "reason": str(error)})
        else:
            raise ValueError("already-exposed interval admitted as final")
    print(
        json.dumps(
            {
                "registry": str(REGISTRY_ROOT),
                "recorded": rows,
                "final_relabel_denials": denials,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
