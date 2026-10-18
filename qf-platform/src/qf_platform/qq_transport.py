"""Small OneBot HTTP client and bounded Tencent image retrieval, without retries."""

from __future__ import annotations

import ipaddress
import json
import socket
import urllib.error
import urllib.request
from typing import Any, cast
from urllib.parse import urlsplit

from .chart_reports import MAX_IMAGE_BYTES
from .qq_protocol import QQConfig
from .storage import PlatformError


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class OneBot:
    def __init__(self, config: QQConfig) -> None:
        self.config = config
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )

    def call(self, action: str, parameters: dict[str, Any]) -> dict[str, Any]:
        if action not in {
            "get_login_info",
            "get_status",
            "get_image",
            "send_private_msg",
            "send_group_msg",
            "get_group_info",
            "upload_private_file",
        }:
            raise PlatformError("QQ_ACTION_FORBIDDEN")
        if action in {"send_group_msg", "get_group_info"} and (
            not self.config.group_enabled
            or str(parameters.get("group_id")) not in self.config.allowed_groups
        ):
            raise PlatformError("QQ_GROUP_DISABLED")
        request = urllib.request.Request(
            self.config.api_url + "/" + action,
            data=json.dumps(parameters).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.config.token,
            },
        )
        try:
            with self.opener.open(request, timeout=15) as response:
                raw = response.read(256001)
            if len(raw) > 256000:
                raise ValueError("bounded response required")
            value = json.loads(raw)
            if value.get("status") != "ok" or value.get("retcode") != 0:
                raise PlatformError("QQ_ACTION_REJECTED")
            return cast(dict[str, Any], value)
        except PlatformError:
            raise
        except (OSError, ValueError, TypeError, AttributeError) as error:
            raise PlatformError("QQ_ACTION_EFFECT_UNKNOWN") from error

    def image(self, segment: dict[str, Any]) -> bytes:
        url = segment.get("url")
        if not url:
            value = self.call(
                "get_image", {"file": str(segment.get("file", ""))[:1000]}
            )
            result = value.get("data")
            url = result.get("url") if isinstance(result, dict) else None
        return download_image(url, allow_fake_ip_cdn=self.config.allow_fake_ip_cdn)


def download_image(url: Any, *, allow_fake_ip_cdn: bool = False) -> bytes:
    if not isinstance(url, str) or len(url) > 8192:
        raise PlatformError("QQ_IMAGE_URL_INVALID")
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if (
        parsed.scheme not in {"https", "http"}
        or not (
            host == "multimedia.nt.qq.com"
            or host == "multimedia.nt.qq.com.cn"
            or host == "qpic.cn"
            or host.endswith(".qpic.cn")
        )
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 80, 443}
        or parsed.fragment
    ):
        raise PlatformError("QQ_IMAGE_URL_INVALID")
    if parsed.scheme == "http":
        url = (
            "https://"
            + host
            + parsed.path
            + ("?" + parsed.query if parsed.query else "")
        )
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)

        def permitted(address: str | int) -> bool:
            ip = ipaddress.ip_address(address)
            return ip.is_global or (
                allow_fake_ip_cdn and ip in ipaddress.ip_network("198.18.0.0/15")
            )

        if not addresses or any(not permitted(x[4][0]) for x in addresses):
            raise PlatformError("QQ_IMAGE_URL_INVALID")
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )
        with opener.open(
            urllib.request.Request(url, headers={"User-Agent": "QuantForge-QQ/1"}),
            timeout=20,
        ) as response:
            data = response.read(MAX_IMAGE_BYTES + 1)
        if not 32 <= len(data) <= MAX_IMAGE_BYTES:
            raise PlatformError("CHART_IMAGE_SIZE_INVALID")
        return cast(bytes, data)
    except OSError as error:
        raise PlatformError("QQ_IMAGE_DOWNLOAD_FAILED") from error
