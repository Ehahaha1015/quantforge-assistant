"""Bounded read-only historical GET; Core qualifies and retains the bytes."""

from __future__ import annotations

import time
import urllib.error
import urllib.request

from quantforge.historical.alphavantage import HistoricalDataError

_URL = "https://www.alphavantage.co/query?function=TIME_SERIES_DAILY&symbol=IBM&apikey=demo"
_MAX_BYTES = 2_000_000


def fetch_public_demo() -> bytes:
    """Bounded, fixed HTTPS GET; retry only an idempotent transient response."""
    request = urllib.request.Request(
        _URL, headers={"User-Agent": "QuantForge-research/0.1"}
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                if response.geturl() != _URL or response.status != 200:
                    raise HistoricalDataError(
                        "unexpected provider response target/status"
                    )
                raw = response.read(_MAX_BYTES + 1)
                if len(raw) > _MAX_BYTES:
                    raise HistoricalDataError("oversized provider response")
                return raw
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 502, 503, 504) and attempt < 2:
                time.sleep(attempt + 1)
                continue
            category = (
                "rate_limited"
                if exc.code == 429
                else "rejected"
                if exc.code < 500
                else "unavailable"
            )
            raise HistoricalDataError(f"historical provider {category}") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise HistoricalDataError("historical provider unavailable") from None
    raise HistoricalDataError("historical provider unavailable")
