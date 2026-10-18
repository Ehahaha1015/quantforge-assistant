"""Bounded private-message OneBot 11 inputs; never infer confirmed chart metadata."""

from __future__ import annotations

import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .chart_reports import ChartRequest
from .storage import PlatformError

HELP = (
    "股票截图助手（日线未来5根）\n"
    "请使用能辨认证券代码、日期和日K标识的截图；五日分时图不适用。\n"
    "发一张走势图，再发：\n"
    "/分析 600519 2026-09-01 2026-09-30 不复权 持仓=5\n"
    "也可把该文字与单张截图同发。复权可填：不复权/前复权/后复权/未知。\n"
    "可选：成本=100 已持=2；持仓观察可选3/5/10/20根，每根复评。\n"
    "/状态 查看自己的最新任务；/报告 chart-任务编号 重新领取已有结果（不调用模型）。\n"
    "截图与参数不符时暂停精确参考；效果未验证，不保证获利，不进行交易。"
)


def failure_reply(code: str) -> str:
    if code == "QQ_IMAGE_URL_INVALID":
        return (
            "QQ图片下载地址未通过检查，此次尚未调用识图模型或新增模型费用。\n"
            "请联系维护者检查收图通道，并保留这张图片。"
        )
    if code == "QQ_IMAGE_DOWNLOAD_FAILED":
        return (
            "暂时无法下载这张QQ图片，此次尚未调用识图模型或新增模型费用。\n"
            "请稍后重新发送原图，系统不会自动重试。"
        )
    return f"未创建或完成新分析（{code[:80]}），不会自动重试。\n" + HELP


def qq_id(value: Any) -> str:
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]{4,19}", str(value)):
        raise PlatformError("QQ_ID_INVALID")
    return str(value)


@dataclass(frozen=True)
class QQConfig:
    bot_id: str
    allowed_users: tuple[str, ...]
    token: str
    api_url: str = "http://napcat:3000"
    enabled: bool = False
    daily_limit: int = 3
    allow_fake_ip_cdn: bool = False
    personal_enabled: bool = False
    group_enabled: bool = False
    allowed_groups: tuple[str, ...] = ()
    group_allowed_users: tuple[str, ...] = ()

    @classmethod
    def load(cls, path: Path) -> QQConfig:
        if path.is_symlink() or path.stat().st_size > 16384:
            raise PlatformError("QQ_CONFIG_INVALID")
        value = json.loads(path.read_bytes())
        if not isinstance(value, dict):
            raise PlatformError("QQ_CONFIG_INVALID")
        bot = qq_id(value["bot_id"]) if value.get("bot_id") else ""
        users = tuple(qq_id(x) for x in value.get("allowed_users", []))
        groups = tuple(qq_id(x) for x in value.get("allowed_groups", []))
        group_users = tuple(qq_id(x) for x in value.get("group_allowed_users", []))
        token = value.get("token", "")
        url = value.get("api_url", "http://napcat:3000")
        parsed = urlsplit(url)
        if (
            not isinstance(token, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{32,100}", token)
            or parsed.scheme != "http"
            or parsed.hostname not in {"napcat", "127.0.0.1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or type(value.get("enabled", False)) is not bool
            or type(value.get("daily_limit", 3)) is not int
            or type(value.get("allow_fake_ip_cdn", False)) is not bool
            or type(value.get("personal_enabled", False)) is not bool
            or type(value.get("group_enabled", False)) is not bool
            or not 1 <= value.get("daily_limit", 3) <= 20
            or len(users) > 20
            or len(groups) > 1
            or len(group_users) > 20
            or bot in group_users
            or (
                value.get("group_enabled", False)
                and (
                    not value.get("personal_enabled", False)
                    or not groups
                    or not group_users
                )
            )
        ):
            raise PlatformError("QQ_CONFIG_INVALID")
        return cls(
            bot,
            users,
            token,
            url.rstrip("/"),
            value.get("enabled", False),
            value.get("daily_limit", 3),
            value.get("allow_fake_ip_cdn", False),
            value.get("personal_enabled", False),
            value.get("group_enabled", False),
            groups,
            group_users,
        )

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.bot_id and self.allowed_users)


def verify_signature(raw: bytes, signature: str, token: str) -> None:
    # NapCat HTTP clients sign the exact request bytes with HMAC-SHA1.
    expected = "sha1=" + hmac.new(token.encode(), raw, "sha1").hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise PlatformError("QQ_EVENT_UNAUTHENTICATED")


def _message_event(event: dict[str, Any], config: QQConfig) -> bool:
    if not config.active or str(event.get("self_id")) != config.bot_id:
        return False
    if event.get("post_type") != "message":
        return False
    at = event.get("time")
    if type(at) is not int or abs(datetime.now(UTC).timestamp() - at) > 3600:
        return False
    if type(event.get("message_id")) not in (str, int) or not re.fullmatch(
        r"-?[0-9]{1,20}", str(event["message_id"])
    ):
        raise PlatformError("QQ_MESSAGE_INVALID")
    segments = event.get("message")
    if not isinstance(segments, list) or not 1 <= len(segments) <= 30:
        raise PlatformError("QQ_MESSAGE_ARRAY_REQUIRED")
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get("data"), dict):
            raise PlatformError("QQ_MESSAGE_INVALID")
    return True


def private_event(event: dict[str, Any], config: QQConfig) -> bool:
    if (
        event.get("message_type") != "private"
        or str(event.get("user_id")) not in config.allowed_users
        or str(event.get("user_id")) == config.bot_id
    ):
        return False
    return _message_event(event, config)


def group_event(event: dict[str, Any], config: QQConfig) -> bool:
    if (
        not config.group_enabled
        or not config.personal_enabled
        or event.get("message_type") != "group"
        or str(event.get("group_id")) not in config.allowed_groups
        or str(event.get("user_id")) not in config.group_allowed_users
        or str(event.get("user_id")) == config.bot_id
        or event.get("sub_type") != "normal"
        or event.get("anonymous") is not None
    ):
        return False
    return _message_event(event, config) and any(
        x.get("type") == "at" and str(x["data"].get("qq")) == config.bot_id
        for x in event["message"]
    )


def accepted_event(event: dict[str, Any], config: QQConfig) -> bool:
    return private_event(event, config) or group_event(event, config)


def group_destination(address: str) -> tuple[str, str] | None:
    parts = address.split(":")
    if len(parts) != 3 or parts[0] != "group":
        return None
    try:
        return qq_id(parts[1]), qq_id(parts[2])
    except PlatformError:
        return None


def conversation_owner(event: dict[str, Any]) -> str:
    sender = qq_id(event.get("user_id"))
    return (
        "group:" + qq_id(event.get("group_id")) + ":" + sender
        if event.get("message_type") == "group"
        else sender
    )


def message_parts(event: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    text = "".join(
        str(x["data"].get("text", ""))
        for x in event["message"]
        if x.get("type") == "text"
    ).strip()
    images = [x["data"] for x in event["message"] if x.get("type") == "image"]
    if len(text) > 1600 or len(images) > 1:
        raise PlatformError("QQ_ONE_IMAGE_REQUIRED")
    return text, images


def chart_metadata(text: str) -> dict[str, Any]:
    values = text.split()
    if (
        len(values) < 5
        or values[0] not in {"/分析", "分析"}
        or not re.fullmatch(r"[0-9]{6}", values[1])
    ):
        raise PlatformError("QQ_CHART_PARAMETERS_REQUIRED")
    adjustments = {"不复权": "RAW", "前复权": "QFQ", "后复权": "HFQ", "未知": "UNKNOWN"}
    if values[4] not in adjustments:
        raise PlatformError("QQ_CHART_PARAMETERS_REQUIRED")
    result: dict[str, Any] = {
        "symbol": values[1],
        "period_start": values[2],
        "period_end": values[3],
        "adjustment": adjustments[values[4]],
        "timeframe": "1d",
        "horizon_bars": 5,
        "max_holding_bars": 5,
        "review_interval_bars": 1,
        "channel": "QQ",
    }
    seen = set()
    for item in values[5:]:
        name, separator, value = item.partition("=")
        if not separator or name in seen or name not in {"持仓", "成本", "已持"}:
            raise PlatformError("QQ_CHART_PARAMETERS_REQUIRED")
        seen.add(name)
        if name == "成本":
            result["purchase_price"] = value
        else:
            if not re.fullmatch(r"[0-9]{1,5}", value):
                raise PlatformError("QQ_CHART_PARAMETERS_REQUIRED")
            result["max_holding_bars" if name == "持仓" else "held_bars"] = int(value)
    try:
        ChartRequest.model_validate({**result, "image_base64": "A" * 24})
    except ValueError as error:
        raise PlatformError("QQ_CHART_PARAMETERS_REQUIRED") from error
    return result
