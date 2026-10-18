"""Transport-neutral image messages; QQ authentication remains a separate adapter."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Protocol

from .chart_reports import ChartRequest
from .chart_timeframes import horizon_text
from .storage import PlatformError, digest


@dataclass(frozen=True)
class IncomingChartImage:
    channel: str
    sender_id: str
    message_id: str
    image_bytes: bytes

    def request_key(self) -> str:
        if self.channel != "QQ" or not self.sender_id or not self.message_id:
            raise PlatformError("CHART_CHANNEL_ENVELOPE_INVALID")
        if len(self.sender_id) > 200 or len(self.message_id) > 200:
            raise PlatformError("CHART_CHANNEL_ENVELOPE_INVALID")
        return "qq-" + digest([self.channel, self.sender_id, self.message_id])

    def normalize(self, confirmed_metadata: dict[str, Any]) -> dict[str, Any]:
        return ChartRequest.model_validate(
            {
                **confirmed_metadata,
                "channel": "QQ",
                "image_base64": base64.b64encode(self.image_bytes).decode(),
            }
        ).model_dump(mode="json")


class ChartMessageTransport(Protocol):
    """A later authenticated QQ adapter implements delivery and account routing."""

    def send_report(
        self, recipient: str, text: str, markdown_document: bytes
    ) -> None: ...


def short_reply(snapshot: dict[str, Any]) -> str:
    if snapshot["status"] != "SUCCEEDED" or snapshot.get("report") is None:
        return "截图分析尚未完成，请核查任务状态。结果未知时不会自动重发模型请求。"
    report = snapshot["report"]
    from .chart_conclusions import conclusion

    combined = conclusion(snapshot)
    if combined:
        return (
            f"{snapshot['request']['symbol']} 走势图报告\n"
            + str(combined["headline"])
            + "\n"
            + "\n".join(combined["reasons"])
            + "\n"
            + str(combined["validation_note"])
            + (
                "\n" + str(combined["uncertainty_note"])
                if combined.get("uncertainty_note")
                else ""
            )
        )
    direction = {
        "BULLISH": "偏强",
        "RANGE": "震荡",
        "BEARISH": "偏弱",
        "UNCLEAR": "无法判断",
    }
    return (
        f"{snapshot['request']['symbol']} 走势图报告\n"
        f"{horizon_text(snapshot['request'])}\n"
        f"图中趋势：{direction[report['observed_trend']]}\n"
        f"条件性展望：{direction[report['outlook']]}\n"
        + str(report["summary"])[:1000]
        + "\n仅为截图解读与条件性情景，未经预测准确性验证，不构成投资建议。"
    )
