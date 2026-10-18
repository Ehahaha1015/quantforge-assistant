from pathlib import Path

from test_room import room

from qf_lab.context_view import bounded_context
from qf_lab.gateway import canonical


def test_full_retained_evidence_is_unchanged_and_prompt_sampling_is_explicit():
    retained = [{"window": i, "sharpe": -i} for i in range(172)]
    context = {
        "evidence": [{"evidence_id": "E", "FACT": {"validation_windows": retained}}],
        "diagnostics": {"trials": [{"evidence_id": "E", "validation_windows": retained}]},
    }
    supplied = bounded_context(context)
    for facts in (supplied["evidence"][0]["FACT"], supplied["diagnostics"]["trials"][0]):
        assert len(facts["validation_windows"]) == 12
        view = facts["validation_window_view"]
        assert view["retained_count"] == 172 and view["full_evidence_id"] == "E"
        assert view["omitted_windows_are_not_assumed_pass"] is True
        assert facts["validation_windows"] == [retained[i] for i in view["supplied_indices"]]
        assert facts["validation_windows"][0] == retained[0]
        assert facts["validation_windows"][-1] == retained[-1]
    assert context["evidence"][0]["FACT"]["validation_windows"] == retained
    assert len(retained) == 172
    assert bounded_context(supplied) == supplied


def test_multiyear_research_round_fits_gateway_bound_without_final_data(tmp_path: Path):
    app = room(tmp_path, rounds=1, n=863, calls=6)
    state = app.run()
    assert state["status"] == "COMPLETE"
    assert state["rounds_completed"] == 1 and state["budget"]["spent_calls"] == 6
    prompts = [v["context"] for v in state["calls"] if v["phase"] == "REQUEST"]
    assert max(len(canonical(v).encode()) for v in prompts) < 45000
    for prompt in prompts:
        for pack in prompt.get("evidence", []):
            assert pack["FACT"]["final_lockbox_scored"] is False
            assert pack["FACT"]["validation_window_view"]["retained_count"] == 172
        assert "final_metrics" not in canonical(prompt)
