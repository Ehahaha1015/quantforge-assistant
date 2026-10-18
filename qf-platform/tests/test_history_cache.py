import gzip
import json
from datetime import date

import pytest
from quantforge.strategies.daily_validation import build_history
from test_chart_synthesis import daily_rows, raw

from qf_platform import history_cache
from qf_platform.chart_algorithms import parse_daily


def test_verified_history_checkpoint_is_equal_and_skips_completed_symbol(
    tmp_path, monkeypatch
):
    frame = parse_daily(
        raw(daily_rows()), "sh600519", date(2026, 6, 1), date(2026, 9, 8)
    ).head(85)
    expected = build_history(frame, None, frame)
    first, profile = history_cache.cached_history(frame, None, frame, tmp_path)
    assert first == expected and profile["cache_hit"] is False
    monkeypatch.setattr(
        history_cache,
        "build_history",
        lambda *args: pytest.fail("completed symbol recomputed"),
    )
    second, profile = history_cache.cached_history(frame, None, frame, tmp_path)
    assert second == expected and profile["cache_hit"] is True
    path = next((tmp_path / "history-cache").glob("*.gz"))
    value = json.loads(gzip.decompress(path.read_bytes()))
    value["history"][next(iter(value["history"]))]["inputs"]["as_of_bar"] = "tampered"
    path.write_bytes(gzip.compress(json.dumps(value).encode()))
    with pytest.raises(ValueError, match="digest"):
        history_cache.cached_history(frame, None, frame, tmp_path)
    path.write_bytes(b"\x1f\x8b")
    with pytest.raises(ValueError, match="incomplete"):
        history_cache.cached_history(frame, None, frame, tmp_path)


def test_changed_runtime_never_reuses_old_history_checkpoint(tmp_path, monkeypatch):
    frame = parse_daily(
        raw(daily_rows()), "sh600519", date(2026, 6, 1), date(2026, 9, 8)
    ).head(1)
    calls = []

    def compute(*args):
        calls.append(1)
        return {}

    monkeypatch.setattr(history_cache, "build_history", compute)
    history_cache.cached_history(frame, None, None, tmp_path)
    history_cache.cached_history(frame, None, None, tmp_path)
    assert len(calls) == 1
    monkeypatch.setattr(
        history_cache, "runtime_identity", lambda: {"python": "different-runtime"}
    )
    _, profile = history_cache.cached_history(frame, None, None, tmp_path)
    assert profile["cache_hit"] is False and len(calls) == 2
