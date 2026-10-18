import pytest

from quantforge.strategies.chart_decision import GROUP_IDS, context_relation, decide


def groups(directions=("BULLISH", "RANGE", "RANGE")):
    return [
        {"id": key, "direction": direction, "internal_conflict": False}
        for key, direction in zip(GROUP_IDS, directions, strict=True)
    ]


def test_all_variants_share_the_same_abstention_and_risk_rules():
    evidence = groups()
    assert decide(evidence, "TREND", [])["direction"] == "UNCLEAR"
    assert (
        decide(evidence, "TREND", [], ("BREAKOUT", "REVERSION"))["direction"]
        == "UNCLEAR"
    )
    assert decide(evidence, "TREND", [], ("TREND",))["direction"] == "BULLISH"
    assert decide(evidence, "RANGE", [], ("BREAKOUT",))["direction"] == "RANGE"
    evidence[0]["internal_conflict"] = True
    assert decide(evidence, "RANGE", [], ("TREND",))["direction"] == "UNCLEAR"
    assert (
        decide(groups(), "RANGE", ["DATA_INVALID"], ("BREAKOUT",))["direction"]
        == "UNCLEAR"
    )
    with pytest.raises(ValueError):
        decide(groups(), "RANGE", [], ("TREND", "TREND"))


def test_context_data_missing_is_distinct_from_valid_abstention_and_neutral():
    def context(direction, status="AVAILABLE", count=8):
        return {
            "status": status,
            "synthesis": {"direction": direction, "coverage": {"computed": count}},
        }

    assert context_relation("BULLISH", [context("UNCLEAR")]) == "UNCONFIRMED"
    assert context_relation("BULLISH", [context("RANGE")]) == "NEUTRAL"
    assert context_relation("BULLISH", [context("BEARISH")]) == "CONFLICT"
    assert context_relation("BULLISH", [context("BULLISH")]) == "ALIGNED"
    assert context_relation("BULLISH", [context("UNCLEAR", count=0)]) == "MISSING"
    assert context_relation("BULLISH", []) == "SINGLE_PERIOD"
