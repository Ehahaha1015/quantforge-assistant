"""Explicit conservative information-interval purging and chronological splits.

Indices refer to one immutable chronological observation index. All intervals
are closed. This utility does not relabel a generic time gap as full purging.
It conservatively removes any overlapping information, including shared
feature history; callers must state the actual experiment horizon separately.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from quantforge.experiments import canonical_json


def _index(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10000000


@dataclass(frozen=True)
class InformationInterval:
    sample_id: str
    feature_start: int
    feature_end: int
    decision_index: int
    label_start: int
    label_end: int

    def __post_init__(self) -> None:
        if (not isinstance(self.sample_id, str) or not self.sample_id
                or not all(_index(value) for value in (
                    self.feature_start, self.feature_end, self.decision_index,
                    self.label_start, self.label_end))
                or not self.feature_start <= self.feature_end <= self.decision_index
                or not self.decision_index <= self.label_start <= self.label_end):
            raise ValueError("invalid causal feature/decision/label interval")

    @property
    def information_start(self) -> int:
        return self.feature_start

    @property
    def information_end(self) -> int:
        return self.label_end


def purge_and_embargo(
    training: Sequence[InformationInterval],
    validation: Sequence[InformationInterval],
    *,
    embargo_bars: int,
) -> dict[str, Any]:
    """Remove closed information overlap and post-validation decision embargo.

    Full information interval [feature_start,label_end] overlap is a declared
    conservative rule. Embargo blocks decisions in (last_label_end,
    last_label_end+embargo_bars]. Embargo zero blocks no extra decisions.
    """
    if not _index(embargo_bars) or not validation:
        raise ValueError("nonempty validation and nonnegative embargo required")
    combined = list(training) + list(validation)
    if len(combined) > 10000 or len(training) * len(validation) > 1000000:
        raise ValueError("interval comparison resource bound exceeded")
    if (any(not isinstance(item, InformationInterval) for item in combined)
            or len({item.sample_id for item in combined}) != len(combined)):
        raise ValueError("interval identities must be unique and valid")
    end = max(item.label_end for item in validation)
    kept, purged, embargoed = [], [], []
    for item in training:
        overlap = any(item.information_start <= other.information_end
                      and other.information_start <= item.information_end
                      for other in validation)
        if overlap:
            purged.append(item.sample_id)
        elif end < item.decision_index <= end + embargo_bars:
            embargoed.append(item.sample_id)
        else:
            kept.append(item.sample_id)
    protocol = {"training": [asdict(item) for item in training],
                "validation": [asdict(item) for item in validation],
                "embargo_bars": embargo_bars,
                "interval_semantics": "closed full-information overlap",
                "embargo_semantics": "decision index in (last label end, end+embargo]"}
    return {"kept": kept, "purged": purged, "embargoed": embargoed,
            "protocol": protocol,
            "partition_sha256": hashlib.sha256(canonical_json(protocol).encode()).hexdigest()}


def walk_forward_windows(
    total_bars: int,
    *,
    initial_history_bars: int,
    window_bars: int,
    gap_bars: int = 0,
) -> dict[str, Any]:
    """Freeze expanding training-prefix windows with an explicit unscored gap.

    Boundaries are half-open. OOS windows are adjacent after the first gap;
    each training prefix ends gap_bars before its OOS start. A gap alone does
    not assert label purging or embargo. No future bars enter a training prefix.
    """
    if (not all(_index(value) for value in (
            total_bars, initial_history_bars, window_bars, gap_bars))
            or initial_history_bars < 1 or window_bars < 1
            or initial_history_bars + gap_bars >= total_bars):
        raise ValueError("invalid chronological window policy")
    if (total_bars - initial_history_bars - gap_bars + window_bars - 1) // window_bars > 10000:
        raise ValueError("chronological window resource bound exceeded")
    windows: list[dict[str, int]] = []
    start = initial_history_bars + gap_bars
    while start < total_bars:
        end = min(total_bars, start + window_bars)
        windows.append({"index": len(windows), "history_start": 0,
                        "history_end_exclusive": start - gap_bars,
                        "gap_start": start - gap_bars,
                        "gap_end_exclusive": start,
                        "validation_start": start,
                        "validation_end_exclusive": end})
        start = end
    protocol = {"schema_version": "scale-walk-forward-v1", "total_bars": total_bars,
                "initial_history_bars": initial_history_bars,
                "window_bars": window_bars, "gap_bars": gap_bars,
                "boundaries": "half-open", "windows": windows,
                "purging_claim": False, "embargo_claim": False}
    return {**protocol,
            "partition_sha256": hashlib.sha256(canonical_json(protocol).encode()).hexdigest()}
