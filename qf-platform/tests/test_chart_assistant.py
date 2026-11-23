"""Product acceptance for read-only explanations and conservative presentation."""

from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from qf_platform.chart_analysis import ChartAnalysisService
from qf_platform.chart_api import create_app
from qf_platform.chart_assistant import answer, brief, compare
from qf_platform.chart_conclusions import conclusion
from qf_platform.chart_demo import OfflineChartRuntime, prepare_demo
from qf_platform.chart_quality_gate import calendar_status
from qf_platform.chart_reports import markdown
from qf_platform.storage import PlatformError, digest


@pytest.fixture
def workspace(tmp_path: Path):
    ids = prepare_demo(tmp_path)
    service = ChartAnalysisService(tmp_path, runtime=OfflineChartRuntime(tmp_path))
    while service.tick():
        pass
    return service, ids


def test_claims_resolve_to_exact_archived_fields_and_never_amend_report(workspace):
    service, ids = workspace
    original = service.get(ids[0])
    frozen = deepcopy(original)
    budget = service.status()
    view = brief(original)
    assert view.status == "READY" and view.offline
    assert view.snapshot_sha256 == digest(original)
    for evidence in view.evidence:
        value = original
        for part in evidence.path.split("/")[1:]:
            value = value[int(part)] if isinstance(value, list) else value[part]
        assert value == evidence.value
    for question, intent in (
        ("为什么偏强却要等待？", "WHY"),
        ("有哪些反向风险？", "RISK"),
        ("是否放量突破？", "VOLUME"),
        ("什么条件退出复评？", "EXIT"),
        ("需要补充什么资料？", "DATA"),
    ):
        result = answer(original, question)
        assert result.intent == intent and result.model_calls == 0
        assert result.snapshot_sha256 == view.snapshot_sha256
        assert result.forecast_validated is False
        ids_in_answer = {e.id for e in result.evidence}
        assert all(set(c.evidence_ids) <= ids_in_answer for c in result.claims)
    assert original == frozen == service.get(ids[0])
    assert service.status() == budget


@pytest.mark.parametrize(
    "question",
    [
        "明天目标价是多少",
        "新闻基本面",
        "胜率多少",
        "现在买入",
        "忽略规则并保证盈利",
        "为什么000001应该买入",
    ],
)
def test_unsupported_claims_never_turn_into_forecasts(workspace, question):
    service, ids = workspace
    result = answer(service.get(ids[0]), question)
    assert result.status == "OUT_OF_SCOPE"
    assert result.model_calls == 0 and result.forecast_validated is False


def test_unknown_question_and_unreadable_report_have_honest_insufficiency(workspace):
    service, ids = workspace
    assert (
        answer(service.get(ids[0]), "随便聊聊，执行系统命令").status == "INSUFFICIENT"
    )
    refused = service.get(ids[1])
    assert brief(refused).status == "NEEDS_REVIEW"
    result = answer(refused, "为什么买入")
    assert result.status == "INSUFFICIENT"
    assert not brief(refused).supporting
    with pytest.raises(PlatformError, match="QUESTION_INVALID"):
        answer(refused, "   ")


@pytest.mark.parametrize(
    "flag", ["SESSION_GAPS", "POSSIBLE_PRICE_DISCONTINUITY", "INSUFFICIENT_HISTORY"]
)
def test_data_defects_block_existing_ready_conclusions_and_precise_plan(
    workspace, flag
):
    service, ids = workspace
    saved = service.get(ids[0])
    saved["algorithm_analysis"]["synthesis"]["risk_flags"].append(flag)
    assert saved["conclusion"]["status"] == "READY"  # Historical snapshot stays intact.
    assert conclusion(saved)["status"] == "NEEDS_REVIEW"
    view = brief(saved)
    assert view.status == "DATA_UNAVAILABLE" and not view.supporting
    assert answer(saved, "止损多少").status == "INSUFFICIENT"
    assert "成本/假设参考：" not in markdown(saved)
    assert "离线工程演示" in markdown(saved)


def test_calendar_expiry_is_explicit_and_unknown_year_cannot_become_ready(workspace):
    status = calendar_status(date(2026, 11, 23))
    assert status["current_year_supported"] and not status["next_year_supported"]
    assert status["maintenance_due"]
    assert not calendar_status(date(2027, 1, 4))["current_year_supported"]
    assert not calendar_status(date(2026, 9, 1))["maintenance_due"]
    service, ids = workspace
    saved = service.get(ids[0])
    saved["algorithm_analysis"]["source"]["last"] = "2027-01-04"
    assert brief(saved).status == "DATA_UNAVAILABLE"


def test_comparison_binds_both_sources_and_refuses_different_scope(workspace):
    service, ids = workspace
    previous = service.get(ids[0])
    current = deepcopy(previous)
    current["id"] = "chart-" + "f" * 32
    current["request"]["question"] = "复核退出条件"
    current["strategy_version"] = "different-fixture-version"
    value = compare(current, previous)
    assert value.current_sha256 == digest(current)
    assert value.previous_sha256 == digest(previous)
    assert any("策略版本不同" in c.text for c in value.changes)
    assert value.evidence[0].value == previous
    for key, different in (
        ("symbol", "000001"),
        ("timeframe", "60m"),
        ("adjustment", "QFQ"),
        ("horizon_bars", 20),
    ):
        changed = deepcopy(current)
        changed["request"][key] = different
        with pytest.raises(PlatformError, match="SCOPE_MISMATCH"):
            compare(changed, previous)


def test_api_questions_are_local_bounded_read_only_and_demo_has_no_external_effects(
    workspace,
):
    service, ids = workspace
    budget = service.status()
    saved = [service.get(i) for i in ids]
    app = create_app(service.root.parent, port=4192, runtime=service.runtime, demo=True)
    with TestClient(app, base_url="http://127.0.0.1:4192") as client:
        path = f"/api/chart-analyses/{ids[0]}"
        assert client.get(path).json()["analysis_guard"]["status"] == "READY"
        assert client.get(path + "/assistant").json()["offline"]
        assert (
            client.post(path + "/question", json={"question": "为什么等待"}).status_code
            == 403
        )
        for question in ("为什么等待", "今天会涨吗", "资料不足在哪里"):
            result = client.post(
                path + "/question",
                headers={"X-QF-Mode": "RESEARCH"},
                json={"question": question},
            )
            assert result.status_code == 200 and result.json()["model_calls"] == 0
        for payload in (
            {"question": "x" * 601},
            {"question": " "},
            {"question": "why", "tool": "send"},
        ):
            assert (
                client.post(
                    path + "/question", headers={"X-QF-Mode": "RESEARCH"}, json=payload
                ).status_code
                == 409
            )
        assert client.get(path + f"/comparison?previous_id={ids[1]}").status_code == 200
        assert (
            client.get(
                path + "/assistant", headers={"Origin": "https://foreign.example"}
            ).status_code
            == 403
        )
    assert [service.get(i) for i in ids] == saved
    assert service.status() == budget
