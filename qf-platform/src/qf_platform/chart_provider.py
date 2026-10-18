"""DeepSeek image requests with bounded responses and no automatic retries."""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.error
import urllib.request
from typing import Any

from pydantic import ValidationError

from .chart_reports import ChartReport, instructions
from .storage import PlatformError

MODEL = "deepseek-flash"
ENDPOINT = "https://api.deepseek.com/chat/completions"
MAX_OUTPUT = 5000


class UnknownChartCall(PlatformError):
    """The provider may have consumed this request. Never automatically replay."""


class ProviderFailure(PlatformError):
    """Bounded private response plus safe diagnostics; never a usable report."""

    def __init__(self, code: str, reason: str, raw: bytes, **details: Any) -> None:
        super().__init__(code)
        self.raw_response = raw[:256001]
        self.diagnostics: dict[str, Any] = {
            "schema_version": "chart-provider-diagnostics-v1",
            "reason": reason,
            "response_sha256": hashlib.sha256(self.raw_response).hexdigest(),
            "captured_bytes": len(self.raw_response),
            **details,
        }


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def model_facts(algorithm: dict[str, Any] | None) -> dict[str, Any] | None:
    """Send the frozen conclusion inputs, not every repeated replay metric."""
    if not algorithm or algorithm.get("schema_version") not in {
        "core-chart-strategies-v3",
        "core-chart-strategies-v4",
    }:
        return algorithm
    return {
        key: algorithm.get(key)
        for key in (
            "schema_version",
            "status",
            "reason",
            "timeframe",
            "synthesis",
            "trade_plan",
            "quality",
            "limitations",
            "daily_research",
            "report_plan_replay",
        )
    } | {
        "cross_timeframe": {
            "status": algorithm.get("cross_timeframe", {}).get("status", "MISSING"),
            "items": [
                {
                    "timeframe": x["timeframe"],
                    "status": x["status"],
                    "last_bar": (x.get("source") or {}).get("last"),
                    "direction": (x.get("synthesis") or {}).get("direction", "UNCLEAR"),
                }
                for x in algorithm.get("cross_timeframe", {}).get("items", [])
            ],
        }
    }


def analyze(
    api_key: str,
    image: bytes,
    mime: str,
    metadata: dict[str, Any],
    algorithm_analysis: dict[str, Any] | None = None,
) -> tuple[ChartReport, dict[str, Any]]:
    prompt = {
        "confirmed_by_user": metadata,
        "required_json_schema": ChartReport.model_json_schema(),
        "deterministic_core_analysis": model_facts(algorithm_analysis),
    }
    messages = [
        {"role": "system", "content": instructions()},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": json.dumps(prompt, ensure_ascii=False)},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime};base64,"
                        + base64.b64encode(image).decode(),
                        "detail": "original",
                    },
                },
            ],
        },
    ]
    payload = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": MAX_OUTPUT,
        "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.build_opener(NoRedirect()).open(
            request, timeout=90
        ) as response:
            raw = response.read(256_001)
    except urllib.error.HTTPError as error:
        try:
            rejected = error.read(256001)
        except OSError:
            rejected = b""
        raise ProviderFailure(
            "CHART_PROVIDER_REJECTED",
            "HTTP_REJECTED",
            rejected,
            http_status=error.code,
        ) from error
    except (OSError, TimeoutError) as error:
        raise UnknownChartCall("CHART_PROVIDER_EFFECT_UNKNOWN") from error
    return parse_response(raw)


def parse_response(raw: bytes) -> tuple[ChartReport, dict[str, Any]]:
    """Parse once without repair, replay or network; retain exact failure reasons."""
    if len(raw) > 256000:
        raise ProviderFailure(
            "CHART_PROVIDER_RESPONSE_TOO_LARGE",
            "RESPONSE_TOO_LARGE",
            raw,
            bounded_prefix=True,
        )
    details: dict[str, Any] = {}

    def invalid(reason: str, **extra: Any) -> ProviderFailure:
        return ProviderFailure(
            "CHART_PROVIDER_INVALID_REPORT", reason, raw, **details, **extra
        )

    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as error:
        raise invalid("ENVELOPE_JSON_INVALID") from error
    try:
        choice = value["choices"][0]
        finish = choice["finish_reason"]
        content = choice["message"]["content"]
        usage = value.get("usage", {})
    except (KeyError, IndexError, TypeError, AttributeError) as error:
        raise invalid("ENVELOPE_INVALID") from error
    details["finish_reason"] = (
        finish
        if isinstance(finish, str)
        and finish
        in {
            "stop",
            "length",
            "content_filter",
            "tool_calls",
            "insufficient_system_resource",
        }
        else "OTHER"
    )
    if isinstance(usage, dict):
        for key in ("prompt_tokens", "completion_tokens"):
            count = usage.get(key)
            if type(count) is int and 0 <= count <= 1000000000:
                details[key] = count
    if finish != "stop":
        raise invalid(
            "OUTPUT_TRUNCATED" if finish == "length" else "FINISH_REASON_INVALID"
        )
    if not isinstance(content, str) or not content.strip():
        raise invalid("EMPTY_CONTENT" if not content else "CONTENT_TYPE_INVALID")
    try:
        body = json.loads(content)
    except (ValueError, UnicodeError) as error:
        raise invalid("CONTENT_JSON_INVALID") from error
    try:
        report = ChartReport.model_validate(body)
    except ValidationError as error:
        fields = set(ChartReport.model_fields) | {
            "observation",
            "image_region",
            "direction",
            "trigger",
            "interpretation",
            "invalidation",
        }
        errors = [
            {
                "field": ".".join(
                    str(x) if isinstance(x, int) or x in fields else "unknown_field"
                    for x in item["loc"]
                ),
                "type": item["type"],
            }
            for item in error.errors(
                include_input=False, include_context=False, include_url=False
            )[:12]
        ]
        raise invalid("SCHEMA_INVALID", validation_errors=errors) from error
    try:
        if not isinstance(usage, dict):
            raise ValueError("Invalid usage")
        counts = {
            key: usage.get(key, 0) for key in ("prompt_tokens", "completion_tokens")
        }
        if not all(type(number) is int and number >= 0 for number in counts.values()):
            raise ValueError("Invalid token counts")
    except (ValueError, KeyError, IndexError, TypeError) as error:
        raise invalid("USAGE_INVALID") from error
    return report, {
        "model": str(value.get("model", MODEL))[:120],
        "response_sha256": hashlib.sha256(raw).hexdigest(),
        **counts,
    }
