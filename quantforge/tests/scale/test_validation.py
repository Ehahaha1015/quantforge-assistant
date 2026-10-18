from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from quantforge.product.scale.validation import (
    InformationInterval,
    purge_and_embargo,
    walk_forward_windows,
)


def interval(identity: str, feature: int, decision: int, label_end: int) -> InformationInterval:
    return InformationInterval(identity, feature, decision, decision, decision, label_end)


def test_label_overlap_and_conservative_shared_information_are_purged() -> None:
    validation = [interval("v", 10, 12, 15)]
    training = [interval("old", 0, 2, 3), interval("overlap-label", 4, 8, 12),
                interval("shared-feature", 9, 18, 19), interval("future", 16, 18, 19)]
    actual = purge_and_embargo(training, validation, embargo_bars=0)
    assert actual["kept"] == ["old", "future"]
    assert actual["purged"] == ["overlap-label", "shared-feature"]
    assert actual["embargoed"] == []


def test_embargo_closed_right_boundary_is_separate_from_purge() -> None:
    validation = [interval("v", 10, 12, 15)]
    training = [interval("at-label-end", 15, 15, 15),
                interval("after-one", 16, 16, 16),
                interval("after-two", 17, 17, 17),
                interval("after-three", 18, 18, 18)]
    actual = purge_and_embargo(training, validation, embargo_bars=2)
    assert actual["purged"] == ["at-label-end"]
    assert actual["embargoed"] == ["after-one", "after-two"]
    assert actual["kept"] == ["after-three"]
    assert purge_and_embargo(training, validation, embargo_bars=0)["embargoed"] == []


def test_disjoint_validation_windows_do_not_create_invented_middle_purge() -> None:
    validation = [interval("v1", 10, 11, 12), interval("v2", 20, 21, 22)]
    training = [interval("middle", 15, 16, 17)]
    assert purge_and_embargo(training, validation, embargo_bars=2)["kept"] == ["middle"]


def test_interval_future_feature_and_invalid_horizons_fail_closed() -> None:
    with pytest.raises(ValueError, match="causal"):
        InformationInterval("future-feature", 0, 12, 11, 11, 13)
    with pytest.raises(ValueError, match="causal"):
        InformationInterval("bad-label", 0, 9, 10, 8, 11)
    with pytest.raises(ValueError, match="causal"):
        InformationInterval("negative", -1, 9, 10, 10, 11)
    with pytest.raises(ValueError, match="causal"):
        InformationInterval("bool", True, 9, 10, 10, 11)
    item = interval("frozen", 0, 2, 3)
    with pytest.raises(FrozenInstanceError):
        item.feature_start = 1  # type: ignore[misc]


def test_purge_policy_and_duplicate_sample_identity_refuse() -> None:
    item = interval("same", 0, 2, 3)
    with pytest.raises(ValueError, match="unique"):
        purge_and_embargo([item], [item], embargo_bars=1)
    with pytest.raises(ValueError, match="nonempty"):
        purge_and_embargo([item], [], embargo_bars=1)
    with pytest.raises(ValueError, match="nonnegative"):
        purge_and_embargo([], [item], embargo_bars=-1)


def test_walk_forward_expanding_prefix_gap_partial_and_frozen_identity() -> None:
    actual = walk_forward_windows(17, initial_history_bars=5, window_bars=4, gap_bars=2)
    assert [(row["history_end_exclusive"], row["validation_start"],
             row["validation_end_exclusive"]) for row in actual["windows"]] == [
                 (5, 7, 11), (9, 11, 15), (13, 15, 17)]
    assert actual["purging_claim"] is False
    assert actual["embargo_claim"] is False
    assert actual == walk_forward_windows(17, initial_history_bars=5, window_bars=4, gap_bars=2)
    changed = walk_forward_windows(17, initial_history_bars=5, window_bars=4, gap_bars=1)
    assert changed["partition_sha256"] != actual["partition_sha256"]
    for row in actual["windows"]:
        assert row["history_end_exclusive"] < row["validation_start"]
        assert row["gap_end_exclusive"] == row["validation_start"]


def test_chronological_prefix_and_information_label_attack_independent_reference() -> None:
    # Independently intersect each explicitly listed closed information interval.
    validation = [interval("v", 4, 5, 8)]
    training = [interval(str(i), i, i, i + 1) for i in range(12)]
    actual = purge_and_embargo(training, validation, embargo_bars=1)
    independent_purge = [str(i) for i in range(12)
                         if set(range(i, i + 2)) & set(range(4, 9))]
    assert actual["purged"] == independent_purge
    assert actual["embargoed"] == ["9"]
    assert actual["kept"] == ["0", "1", "2", "10", "11"]


@pytest.mark.parametrize("total,history,window,gap", [
    (5, 5, 1, 0), (5, 4, 1, 1), (5, 0, 1, 0), (5, 1, 0, 0),
    (5, 1, 1, -1), (True, 1, 1, 0), (5, 1, 1, True),
])
def test_invalid_walk_forward_boundaries_refuse(total: int, history: int, window: int, gap: int) -> None:
    with pytest.raises(ValueError, match="chronological"):
        walk_forward_windows(total, initial_history_bars=history, window_bars=window, gap_bars=gap)


def test_validation_resource_bounds_refuse_before_unbounded_allocation() -> None:
    with pytest.raises(ValueError, match="resource"):
        walk_forward_windows(10002, initial_history_bars=1, window_bars=1)
    with pytest.raises(ValueError, match="causal"):
        interval("excessive-index", 0, 10000001, 10000002)
    training = [interval(str(i), i, i, i) for i in range(1001)]
    validation = [interval("v" + str(i), i + 1001, i + 1001, i + 1001) for i in range(1000)]
    with pytest.raises(ValueError, match="resource"):
        purge_and_embargo(training, validation, embargo_bars=0)
