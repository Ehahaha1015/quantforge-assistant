from datetime import timedelta

from test_chart_quality import sessions

from quantforge.strategies.daily_validation import build_history, observations


def test_history_is_prefix_invariant_and_labels_are_purged_at_window_boundary():
    frame = sessions(95)
    full = build_history(frame, None, frame)
    prefix = build_history(frame.head(88), None, frame.head(88))
    assert {key: full[key] for key in prefix} == prefix
    boundary = frame["timestamp"][90]
    rows = observations(frame, full, frame["timestamp"][79], boundary)
    assert rows and all(row["label_end"] < boundary.isoformat() for row in rows)
    assert not observations(frame, full, boundary + timedelta(days=50), None)
