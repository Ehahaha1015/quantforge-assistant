"""One explicit abstention policy for full, single-group and ablated evidence."""

from __future__ import annotations

from typing import Any

VERSION = "chart-decision-policy-v1"
GROUP_IDS = ("TREND", "BREAKOUT", "REVERSION")


def decide(
    groups: list[dict[str, Any]],
    market_state: str,
    risk_flags: list[str],
    active_groups: tuple[str, ...] = GROUP_IDS,
) -> dict[str, Any]:
    if (
        not active_groups
        or len(set(active_groups)) != len(active_groups)
        or not set(active_groups) <= set(GROUP_IDS)
    ):
        raise ValueError("unique declared active groups required")
    by_id = {x["id"]: x for x in groups}
    if len(by_id) != len(groups) or not set(active_groups) <= set(by_id):
        raise ValueError("active group evidence is missing or duplicated")
    selected = [by_id[x] for x in active_groups]
    if any(
        x["direction"] not in {"BULLISH", "BEARISH", "RANGE", "UNCLEAR"}
        for x in selected
    ):
        raise ValueError("declared direction required")
    reason = None
    if risk_flags:
        direction, reason = "UNCLEAR", "RISK_OR_DATA_FILTER"
    elif any(x["direction"] == "UNCLEAR" for x in selected):
        direction, reason = "UNCLEAR", "INSUFFICIENT_EVIDENCE"
    else:
        up = sum(x["direction"] == "BULLISH" for x in selected)
        down = sum(x["direction"] == "BEARISH" for x in selected)
        conflict = bool(up and down) or any(x["internal_conflict"] for x in selected)
        needed = min(2, len(selected))
        if conflict:
            direction, reason = "UNCLEAR", "SIGNAL_CONFLICT"
        elif up >= needed:
            direction = "BULLISH"
        elif down >= needed:
            direction = "BEARISH"
        elif market_state == "RANGE":
            direction = "RANGE"
        else:
            direction, reason = "UNCLEAR", "TREND_WITHOUT_CONFIRMATION"
    return {
        "direction": direction,
        "abstention_reason": reason,
        "policy_version": VERSION,
        "active_groups": list(active_groups),
        "required_votes": min(2, len(active_groups)),
    }


def context_relation(main: str, contexts: list[dict[str, Any]]) -> str:
    if not contexts:
        return "SINGLE_PERIOD"
    if any(
        x.get("status") != "AVAILABLE"
        or not x.get("synthesis")
        or x["synthesis"].get("coverage", {}).get("computed", 0) < 8
        for x in contexts
    ):
        return "MISSING"
    directions = [x["synthesis"]["direction"] for x in contexts]
    if main in {"BULLISH", "BEARISH"} and any(
        x in {"BULLISH", "BEARISH"} and x != main for x in directions
    ):
        return "CONFLICT"
    if "UNCLEAR" in directions:
        return "UNCONFIRMED"
    if all(x == "RANGE" for x in directions):
        return "NEUTRAL"
    return (
        "ALIGNED"
        if main in {"BULLISH", "BEARISH"} and all(x == main for x in directions)
        else "MIXED"
    )
