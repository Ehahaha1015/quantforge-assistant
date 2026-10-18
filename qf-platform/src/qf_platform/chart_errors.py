"""Bounded user-facing failure causes; no raw provider prose, secrets or URLs."""

from __future__ import annotations

from typing import Any


def failure_message(snapshot: dict[str, Any]) -> str:
    reason = str((snapshot.get("provider_diagnostics") or {}).get("reason", ""))
    explanations = {
        "OUTPUT_TRUNCATED": "模型输出达到长度上限，报告被截断，未生成可用报告。",
        "EMPTY_CONTENT": "识图服务返回空内容，未生成可用报告。",
        "CONTENT_JSON_INVALID": "识图服务返回的报告不是有效JSON，未通过校验。",
        "SCHEMA_INVALID": "识图服务返回的报告字段或取值不符合要求，未通过校验。",
        "RESPONSE_TOO_LARGE": "识图服务返回内容超过接收上限，已停止解析。",
        "ENVELOPE_JSON_INVALID": "识图服务的响应无法解析，未生成可用报告。",
        "ENVELOPE_INVALID": "识图服务缺少必要响应字段，未生成可用报告。",
        "USAGE_INVALID": "识图服务的用量字段异常，报告未通过校验。",
        "CONTENT_TYPE_INVALID": "识图服务返回的报告类型不符合要求。",
        "FINISH_REASON_INVALID": "识图服务未正常完成报告生成。",
        "HTTP_REJECTED": "识图服务拒绝了本次请求。",
    }
    fallback = {
        "CHART_PROVIDER_INVALID_REPORT": (
            "识图服务返回内容未通过格式校验；旧记录未保存具体校验原因。"
        ),
        "CHART_PROVIDER_REJECTED": "识图服务拒绝了本次请求。",
        "CHART_PROVIDER_EFFECT_UNKNOWN": "模型调用结果未知，已停止自动重发。",
    }
    return explanations.get(
        reason, fallback.get(snapshot.get("error", ""), "报告未完成。")
    )
