"""Read-only, source-bound report explanations. No providers, tools or writes."""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .chart_algorithms import symbol_code
from .chart_conclusions import ACTION_NAMES, DIRECTION_NAMES, conclusion
from .chart_metadata import metadata as analysis_metadata
from .chart_quality_gate import quality_blockers
from .storage import PlatformError, digest

QUESTION_LIMIT = 600
BriefStatus = Literal["READY", "NEEDS_REVIEW", "DATA_UNAVAILABLE", "NOT_READY"]
EvidenceKind = Literal["COMPUTATION", "SOURCE", "METADATA", "MODEL_OBSERVATION"]
Intent = Literal["WHY", "RISK", "EXIT", "VOLUME", "DATA", "SCOPE", "OVERVIEW"]
AnswerStatus = Literal["ANSWERED", "INSUFFICIENT", "OUT_OF_SCOPE"]


class Evidence(BaseModel):
    id: str
    label: str
    kind: Literal["COMPUTATION", "SOURCE", "METADATA", "MODEL_OBSERVATION"]
    path: str
    value: Any
    note: str


class Claim(BaseModel):
    text: str
    evidence_ids: list[str]


class ReportBrief(BaseModel):
    schema_version: Literal["chart-assistant-brief-v1"] = "chart-assistant-brief-v1"
    job_id: str
    snapshot_sha256: str
    status: Literal["READY", "NEEDS_REVIEW", "DATA_UNAVAILABLE", "NOT_READY"]
    status_label: str
    as_of: str | None
    offline: bool
    headline: str
    focus_question: str
    supporting: list[Claim]
    opposing: list[Claim]
    next_checks: list[Claim]
    missing: list[Claim]
    evidence: list[Evidence]
    scope_note: str = (
        "解释归档时点的证据；不代表当前行情。固定规则倾向、覆盖率均不是胜率。"
    )
    model_calls: Literal[0] = 0
    forecast_validated: Literal[False] = False


class QuestionCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    question: str = Field(min_length=1, max_length=QUESTION_LIMIT)


class ReportAnswer(BaseModel):
    schema_version: Literal["chart-assistant-answer-v1"] = "chart-assistant-answer-v1"
    job_id: str
    snapshot_sha256: str
    question: str
    intent: Literal["WHY", "RISK", "EXIT", "VOLUME", "DATA", "SCOPE", "OVERVIEW"]
    status: Literal["ANSWERED", "INSUFFICIENT", "OUT_OF_SCOPE"]
    claims: list[Claim]
    evidence: list[Evidence]
    note: str = "仅检索并解释这份报告的归档证据；没有重新识图、取数或调用模型。"
    model_calls: Literal[0] = 0
    forecast_validated: Literal[False] = False


class Comparison(BaseModel):
    schema_version: Literal["chart-report-comparison-v1"] = "chart-report-comparison-v1"
    current_id: str
    previous_id: str
    current_sha256: str
    previous_sha256: str
    changes: list[Claim]
    evidence: list[Evidence]
    note: str = (
        "仅比较已归档报告；时间、问题、持仓及版本可能不同，变化不等于策略改进。"
        "未读取新行情，也未更改任何原报告。"
    )
    model_calls: Literal[0] = 0
    forecast_validated: Literal[False] = False


def brief(snapshot: dict[str, Any]) -> ReportBrief:
    """Derive a view; never amend saved conclusions, targets or model commentary."""
    combined = conclusion(snapshot)
    algorithm = snapshot.get("algorithm_analysis") or {}
    source = algorithm.get("source") or {}
    report = snapshot.get("report") or {}
    meta = analysis_metadata(snapshot)
    entries: list[Evidence] = []

    def cite(path: str, label: str, kind: EvidenceKind = "COMPUTATION") -> str:
        value: Any = snapshot
        for part in path.split("/")[1:]:
            value = value[int(part)] if isinstance(value, list) else value[part]
        key = f"e{len(entries) + 1}"
        entries.append(
            Evidence(
                id=key,
                label=label,
                kind=kind,
                path=path,
                value=value,
                note="归档字段；可追溯不等于外部认证。",
            )
        )
        return key

    meta_id = cite(
        "/resolved_request" if snapshot.get("resolved_request") else "/request",
        "自动识别信息（待核对）"
        if snapshot.get("resolved_request")
        else "用户确认信息（不是图像真值）",
        "METADATA",
    )
    report_id = (
        cite("/report", "模型图像识别（待核对）", "MODEL_OBSERVATION")
        if report
        else None
    )
    supporting: list[Claim] = []
    opposing: list[Claim] = []
    checks: list[Claim] = []
    missing: list[Claim] = []
    blockers = quality_blockers(snapshot)
    state: BriefStatus
    if snapshot["status"] != "SUCCEEDED" or not report:
        state, label = "NOT_READY", "任务尚无可用报告"
    elif blockers or algorithm.get("status") != "AVAILABLE":
        state, label = "DATA_UNAVAILABLE", "数据不足，暂停精确参考"
    elif not combined or combined["status"] != "READY":
        state, label = "NEEDS_REVIEW", "先核对截图信息"
    else:
        state, label = "READY", "可解释归档证据 · 预测未验证"
    if report.get("image_quality") in {"READABLE", "PARTIAL"}:
        if state == "DATA_UNAVAILABLE":
            label = "图像分析可用 · 行情信息待补充"
        elif state == "NEEDS_REVIEW":
            label = "图像分析可用 · 精确价位待核对"
    if state != "READY":
        missing.append(
            Claim(
                text=(combined or {}).get(
                    "alignment_note", "请先完成报告与截图身份核对。"
                ),
                evidence_ids=[meta_id, report_id] if report_id else [meta_id],
            )
        )
        checks.append(
            Claim(
                text="核对图中代码、完整起止日期、K线周期及不复权标识；缺失时补充清晰截图。",
                evidence_ids=[meta_id],
            )
        )
    if source:
        source_id = cite("/algorithm_analysis/source", "归档行情来源", "SOURCE")
        checks.append(
            Claim(
                text=(
                    f"分析截至 {source.get('last', '未知')} 的完整K线；"
                    "要判断现在，需显式进行最新行情复评。"
                ),
                evidence_ids=[source_id],
            )
        )
    if blockers:
        flag_id = cite("/algorithm_analysis", "数据质量与规则快照")
        missing.extend(Claim(text=x, evidence_ids=[flag_id]) for x in blockers)
    if state == "READY" and combined:
        synth = algorithm.get("synthesis") or {}
        synthesis_id = cite("/algorithm_analysis/synthesis", "固定规则综合方向")
        supporting.append(
            Claim(
                text=f"本报告综合倾向为{DIRECTION_NAMES[combined['direction']]}。"
                "单组偏强不等于整体偏强，也不等于已满足购入条件。",
                evidence_ids=[synthesis_id],
            )
        )
        for index, group in enumerate(synth.get("groups", [])):
            eid = cite(f"/algorithm_analysis/synthesis/groups/{index}", group["label"])
            claim = Claim(
                text=f"{group['label']}：{DIRECTION_NAMES[group['direction']]}"
                + ("，组内存在分歧。" if group["internal_conflict"] else "。"),
                evidence_ids=[eid],
            )
            target = (
                supporting
                if (
                    group["direction"] == combined["direction"]
                    and combined["direction"] in {"BULLISH", "BEARISH"}
                    and not group["internal_conflict"]
                )
                else opposing
            )
            target.append(claim)
        plan = algorithm.get("trade_plan") or {}
        if plan:
            plan_id = cite("/algorithm_analysis/trade_plan", "条件性计划与过滤原因")
            supporting.append(
                Claim(
                    text=f"参考动作：{ACTION_NAMES.get(plan['action'], '待核对')}。"
                    + "；".join(plan.get("reasons", [])),
                    evidence_ids=[plan_id],
                )
            )
            checks.extend(
                Claim(text=x, evidence_ids=[plan_id])
                for x in plan.get("exit_rules", [])[:4]
            )
        risk_id = cite("/algorithm_analysis/synthesis/risk_flags", "规则风险标记")
        flags = synth.get("risk_flags", [])
        opposing.append(
            Claim(
                text="规则风险标记："
                + ("、".join(flags) if flags else "本规则未检出，不代表无风险"),
                evidence_ids=[risk_id],
            )
        )
    if report:
        uncertainties_id = cite(
            "/report/uncertainties", "模型声明的不足（待核对）", "MODEL_OBSERVATION"
        )
        missing.extend(
            Claim(text=x, evidence_ids=[uncertainties_id])
            for x in report.get("uncertainties", [])
        )
    if algorithm.get("limitations"):
        eid = cite("/algorithm_analysis/limitations", "计算与数据适用范围")
        missing.extend(
            Claim(text=x, evidence_ids=[eid]) for x in algorithm["limitations"]
        )
    return ReportBrief(
        job_id=snapshot["id"],
        snapshot_sha256=digest(snapshot),
        status=state,
        status_label=label,
        as_of=source.get("last"),
        offline=source.get("provider") == "OFFLINE_SYNTHETIC",
        headline=combined["headline"] if state == "READY" and combined else label,
        focus_question=meta.get("question", ""),
        supporting=supporting,
        opposing=opposing,
        next_checks=checks,
        missing=missing,
        evidence=entries,
    )


def answer(snapshot: dict[str, Any], question: str) -> ReportAnswer:
    question = question.strip()
    if not question or len(question) > QUESTION_LIMIT:
        raise PlatformError("CHART_QUESTION_INVALID")
    view = brief(snapshot)
    intent: Intent = "OVERVIEW"
    status: AnswerStatus = "ANSWERED"
    claims = view.supporting + view.opposing
    mentioned = set(re.findall(r"(?<!\d)\d{6}(?!\d)", question))
    expected_symbol = set(
        re.findall(r"(?<!\d)\d{6}(?!\d)", analysis_metadata(snapshot)["symbol"])
    )
    if mentioned and mentioned != expected_symbol:
        intent, status = "SCOPE", "OUT_OF_SCOPE"
        claims = [
            Claim(
                text="问题涉及另一证券；本次只解释当前报告，不能借用这份证据分析其他股票。",
                evidence_ids=[view.evidence[0].id],
            )
        ]
    elif any(
        x in question.lower()
        for x in (
            "新闻",
            "财报",
            "基本面",
            "长期价值",
            "保证",
            "胜率",
            "概率",
            "下单",
            "市盈率",
            "盈利能力",
            "最新",
            "实时",
            "现在",
            "今天",
            "明天",
            "预测价",
            "目标价",
            "收益率",
            "profit",
            "news",
        )
    ):
        intent, status = "SCOPE", "OUT_OF_SCOPE"
        claims = [
            Claim(
                text="这份归档报告只有指定时点的技术面证据，无法确认实时价格、新闻基本面、未来价格或胜率。"
                "需要最新行情时，请在真实工作区显式点击复评；本次没有取新数据。",
                evidence_ids=[view.evidence[0].id],
            )
        ]
    elif any(
        x in question for x in ("资料", "补充", "数据", "复权", "核对", "不足", "日期")
    ):
        intent = "DATA"
        claims = view.missing + view.next_checks[:1]
    elif view.status != "READY":
        status = "INSUFFICIENT"
        claims = view.missing + view.next_checks
    elif any(x in question for x in ("放量", "量能", "成交量", "突破")):
        intent = "VOLUME"
        candidates = [
            e
            for e in view.evidence
            if e.path.startswith("/algorithm_analysis/synthesis/groups/")
            and isinstance(e.value, dict)
            and e.value.get("id") == "BREAKOUT"
        ]
        claims = [
            Claim(
                text="突破与量能组的归档方向为"
                + DIRECTION_NAMES[e.value["direction"]]
                + "；这组结果不等于已独立核验成交量单位或保证突破延续。",
                evidence_ids=[e.id],
            )
            for e in candidates
        ]
        if not claims:
            status = "INSUFFICIENT"
            claims = [
                Claim(
                    text="本报告缺少可引用的突破与量能组证据，无法确认放量突破。",
                    evidence_ids=[view.evidence[0].id],
                )
            ]
    elif any(
        x in question for x in ("止损", "止盈", "退出", "卖出", "持有", "失效", "复评")
    ):
        intent, claims = "EXIT", view.next_checks
    elif any(x in question for x in ("风险", "反向", "推翻", "分歧")):
        intent, claims = "RISK", view.opposing + view.missing
    elif any(x in question for x in ("为什么", "等待", "买入", "购入", "依据", "偏强")):
        intent, claims = "WHY", view.supporting + view.opposing
    elif not any(
        x in question for x in ("总结", "概览", "概述", "走势", "趋势", "分析")
    ):
        status = "INSUFFICIENT"
        claims = [
            Claim(
                text="暂未识别到可直接核查的报告问题。请具体询问依据、反向风险、量能、退出条件或缺失资料。",
                evidence_ids=[view.evidence[0].id],
            )
        ]
    if not claims:
        status = "INSUFFICIENT"
        claims = [
            Claim(
                text="本报告没有足够的可引用证据回答这个问题。",
                evidence_ids=[view.evidence[0].id],
            )
        ]
    selected = {eid for item in claims for eid in item.evidence_ids}
    return ReportAnswer(
        job_id=view.job_id,
        snapshot_sha256=view.snapshot_sha256,
        question=question,
        intent=intent,
        status=status,
        claims=claims,
        evidence=[e for e in view.evidence if e.id in selected],
    )


def compare(current: dict[str, Any], previous: dict[str, Any]) -> Comparison:
    a, b = analysis_metadata(current), analysis_metadata(previous)
    if current["id"] == previous["id"] or any(
        (
            symbol_code(a["symbol"]) != symbol_code(b["symbol"]),
            a.get("timeframe", "1d") != b.get("timeframe", "1d"),
            a.get("adjustment", "UNKNOWN") != b.get("adjustment", "UNKNOWN"),
            a.get("horizon_bars", a.get("horizon_sessions"))
            != b.get("horizon_bars", b.get("horizon_sessions")),
        )
    ):
        raise PlatformError("CHART_COMPARISON_SCOPE_MISMATCH")
    views = [brief(previous), brief(current)]
    evidence = [
        Evidence(
            id=f"report{i}",
            label="对照报告" if i == 0 else "当前选择的报告",
            kind="SOURCE",
            path="/",
            value=previous if i == 0 else current,
            note="各自对应的完整归档快照，不是新行情。",
        )
        for i, view in enumerate(views)
    ]
    changes = [
        Claim(
            text=f"{view.as_of or '时点未确认'}：{view.headline}",
            evidence_ids=[f"report{i}"],
        )
        for i, view in enumerate(views)
    ]
    for key, label in (
        ("period_end", "截止日期"),
        ("period_start", "开始日期"),
        ("period_end_time", "截止时间"),
        ("purchase_price", "填写成本"),
        ("held_bars", "持有根数"),
        ("question", "重点问题"),
    ):
        if a.get(key) != b.get(key):
            changes.append(
                Claim(
                    text=f"{label}不同：{b.get(key)} → {a.get(key)}。",
                    evidence_ids=["report0", "report1"],
                )
            )
    if current["strategy_version"] != previous["strategy_version"]:
        changes.append(
            Claim(
                text="策略版本不同，不能把变化全部归因于行情。",
                evidence_ids=["report0", "report1"],
            )
        )
    old_source = (previous.get("algorithm_analysis") or {}).get("source") or {}
    new_source = (current.get("algorithm_analysis") or {}).get("source") or {}
    if old_source.get("last") == new_source.get("last") and old_source.get(
        "bars_sha256"
    ) != new_source.get("bars_sha256"):
        changes.append(
            Claim(
                text="同一末根时点的行情摘要不同，需核查数据范围或供应商修订。",
                evidence_ids=["report0", "report1"],
            )
        )
    return Comparison(
        current_id=current["id"],
        previous_id=previous["id"],
        current_sha256=digest(current),
        previous_sha256=digest(previous),
        changes=changes,
        evidence=evidence,
    )


def brief_markdown(snapshot: dict[str, Any]) -> list[str]:
    if "id" not in snapshot or "status" not in snapshot:
        return [
            "## 分析可用性与证据索引",
            "",
            "旧版报告缺少完整任务身份，不生成新的证据摘要；请核对原图与归档时点。",
        ]
    view = brief(snapshot)
    lines = ["## 分析可用性与证据索引", "", view.status_label, view.scope_note]
    if view.offline:
        lines.append(
            "离线工程演示：合成图片、行情与模型输出，不代表真实识图或预测效果。"
        )
    for title, claims in (
        ("支持依据", view.supporting),
        ("反向证据", view.opposing),
        ("下一步核查", view.next_checks),
        ("信息不足", view.missing),
    ):
        lines += ["", f"### {title}", ""]
        lines += [f"- {c.text} [{', '.join(c.evidence_ids)}]" for c in claims]
    lines += ["", f"归档快照 SHA256：{view.snapshot_sha256}", ""]
    lines += [f"- [{e.id}] {e.label}：`{e.path}`" for e in view.evidence]
    return lines
