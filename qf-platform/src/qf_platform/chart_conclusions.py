"""One deterministic final conclusion for the web, Markdown and future transports."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, cast

from .chart_reports import ChartReport, assess_alignment
from .chart_timeframes import LABELS, horizon_text

DIRECTION_NAMES = {
    "BULLISH": "偏强",
    "RANGE": "震荡",
    "BEARISH": "偏弱",
    "UNCLEAR": "证据不足",
}
ACTION_NAMES = {
    "CONSIDER_ENTRY": "条件满足，可考虑购入",
    "WAIT": "等待确认，暂不购入",
    "AVOID": "风险过滤未通过，暂不购入",
    "UNAVAILABLE": "信息不足，先核对",
    "EXIT_REVIEW": "已持仓，优先复核退出条件",
    "HOLD_REVIEW": "已持仓，继续按条件复评",
}


def conclusion(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    algorithm = snapshot.get("algorithm_analysis") or {}
    if algorithm.get("schema_version") not in {
        "core-chart-strategies-v3",
        "core-chart-strategies-v4",
    }:
        return None
    meta = snapshot["request"]
    synthesis = algorithm.get("synthesis") or {}
    plan = algorithm.get("trade_plan") or {}
    report = snapshot.get("report")
    alignment = (
        assess_alignment(meta, ChartReport.model_validate(report))
        if report
        else {"status": "UNCONFIRMED", "note": "截图尚未识别。"}
    )
    stored = snapshot.get("image_alignment")
    if (
        alignment["status"] == "CONSISTENT"
        and stored
        and stored["status"] != "CONSISTENT"
    ):
        alignment = stored
    ready = (
        algorithm.get("status") == "AVAILABLE" and alignment["status"] == "CONSISTENT"
    )
    if snapshot.get("conclusion") is not None and ready:
        return cast(dict[str, Any], deepcopy(snapshot["conclusion"]))
    direction = synthesis.get("direction", "UNCLEAR") if ready else "UNCLEAR"
    action = plan.get("action", "UNAVAILABLE") if ready else "UNAVAILABLE"
    groups = synthesis.get("groups", [])
    supporting = [
        f"{x['label']}：{DIRECTION_NAMES[x['direction']]}"
        for x in groups
        if direction in {"BULLISH", "BEARISH"} and x["direction"] == direction
    ]
    counter = [
        f"{x['label']}：{DIRECTION_NAMES[x['direction']]}"
        + ("，组内有分歧" if x["internal_conflict"] else "")
        for x in groups
        if x["direction"] != direction or x["internal_conflict"]
    ]
    if not supporting:
        supporting = ["尚无足够独立证据支持单一方向。"]
    source = algorithm.get("source") or {}
    cross = algorithm.get("cross_timeframe", {"status": "MISSING", "items": []})
    timeframes = [
        {
            "timeframe": meta.get("timeframe", "1d"),
            "status": algorithm.get("status", "UNAVAILABLE"),
            "direction": synthesis.get("direction", "UNCLEAR"),
            "last_bar": source.get("last"),
            "raw_sha256": source.get("raw_sha256"),
            "reason": algorithm.get("reason"),
        }
    ]
    for item in cross["items"]:
        data = item.get("source") or {}
        timeframes.append(
            {
                "timeframe": item["timeframe"],
                "status": item["status"],
                "direction": (item.get("synthesis") or {}).get("direction", "UNCLEAR"),
                "last_bar": data.get("last"),
                "raw_sha256": data.get("raw_sha256"),
                "reason": item.get("reason"),
            }
        )
    scenarios = (
        [
            {
                "direction": "BULLISH",
                "trigger": "至少两组偏强且无分歧，较长周期与风险确认。",
                "interpretation": "下一根完整K线仍满足时，才复评偏强情景与入场许可。",
                "invalidation": "转弱、分歧或风险触发时撤销；止盈线不是预测目标。",
            },
            {
                "direction": "RANGE",
                "trigger": "区间结构延续，且未形成独立证据组同向的持续突破确认。",
                "interpretation": (
                    "这是区间延续的条件性情景；"
                    "证据不足或周期分歧本身不能证明将会横盘。"
                ),
                "invalidation": "两组同向、风险与较长周期确认后重新判断。",
            },
            {
                "direction": "BEARISH",
                "trigger": "主周期至少两组证据偏弱且无反向分歧。",
                "interpretation": "暂停入场，持仓复核退出；不表示必跌或可即时成交。",
                "invalidation": "偏弱证据消失、出现反向确认或数据不足时撤销该情景。",
            },
        ]
        if ready
        else []
    )
    return {
        "schema_version": "chart-conclusion-v2",
        "uncertainty_note": (
            "市场结构、方向证据与入场许可分别判断。"
            "证据不足表示无法确认方向，不等于预测横盘；"
            "区间状态也不是未来震荡的保证。"
        ),
        "status": "READY" if ready else "NEEDS_REVIEW",
        "direction": direction,
        "action": action,
        "headline": (
            f"{horizon_text(meta)}：{DIRECTION_NAMES[direction]}；{ACTION_NAMES[action]}。"
        )
        if algorithm.get("schema_version") == "core-chart-strategies-v4"
        else (
            f"{LABELS[meta.get('timeframe', '1d')]}{horizon_text(meta)}："
            f"{DIRECTION_NAMES[direction]}；{ACTION_NAMES[action]}。"
        ),
        "alignment_note": alignment["note"],
        "supporting_evidence": supporting,
        "counter_evidence": counter or ["未出现明确反向组；这不证明未来走势确定。"],
        "coverage": synthesis.get("coverage", {"computed": 0, "total": 8}),
        "agreement": synthesis.get("agreement", "INSUFFICIENT"),
        "risk_level": synthesis.get("risk_level", "UNKNOWN"),
        "risk_flags": synthesis.get("risk_flags", []),
        "market_state": synthesis.get("market_state", "INSUFFICIENT"),
        "groups": groups,
        "cross_timeframe_status": cross["status"],
        "timeframes": timeframes,
        "entry_action": plan.get("entry_action", "UNAVAILABLE")
        if ready
        else "UNAVAILABLE",
        "position_action": plan.get("position_action", "NONE")
        if ready
        else "UNAVAILABLE",
        "levels": plan.get("levels") if ready else None,
        "exit_conditions": plan.get("exit_rules", []) if ready else [],
        "reasons": plan.get("reasons", []) if ready else [alignment["note"]],
        "scenarios": scenarios,
        "model_commentary_status": "ARCHIVED_ONLY",
        "forecast_validated": False,
        "validation_note": (
            "固定规则倾向，预测效果未验证；一致性与覆盖率不是概率。"
            "小样本回溯尚未证明稳定优于简单基线。"
        ),
        "daily_research": algorithm.get("daily_research"),
    }
