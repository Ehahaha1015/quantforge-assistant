"""Public screenshot contracts; extensible response fields preserve saved versions."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .chart_reports import ChartReport


class ConfigurationCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    api_key: SecretStr = SecretStr("")
    max_cost_cny: str = Field(pattern=r"^(?:0|[1-9][0-9]{0,3})(?:\.[0-9]{1,2})?$")


class StartCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["start"]


class ReviewCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["review"]


class ExtensibleResponse(BaseModel):
    model_config = ConfigDict(extra="allow")


class AnalysisResponse(ExtensibleResponse):
    schema_version: Literal["chart-analysis-v1"]
    id: str
    status: Literal[
        "WAITING_FOR_MODEL", "QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "UNKNOWN"
    ]
    request: dict[str, Any]
    report: ChartReport | None
    error: str | None
    forecast_validated: Literal[False]


class SettingsResponse(ExtensibleResponse):
    schema_version: Literal["chart-provider-status-v1"]
    configured: bool
    ready: bool
    max_cost_cny: str
    reserved_cny: str
    calls_used: int


class AnalysisListResponse(ExtensibleResponse):
    schema_version: Literal["chart-list-v1"]
    items: list[dict[str, Any]]


class EventsResponse(BaseModel):
    schema_version: Literal["chart-events-v1"] = "chart-events-v1"
    job_id: str
    items: list[dict[str, Any]]
    note: str = "脱敏状态事件；摘要链用于一致性校验，不证明数据库管理员无法改写历史。"
