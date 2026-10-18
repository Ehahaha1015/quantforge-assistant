from __future__ import annotations

from pathlib import Path

import pytest

EXECUTION_SOURCE = Path(__file__).resolve().parents[3] / "src" / "quantforge" / "execution"
PAPER_SOURCE = EXECUTION_SOURCE / "paper"

# Coarse guards: the real guarantee is the pure-function design, but these
# keep accidental capability creep out of the paper execution surface.
FORBIDDEN_EFFECT_TOKENS = (
    "random",
    "uuid",
    "datetime.now",
    "utcnow",
    "time.time",
    "socket",
    "requests",
    "urllib",
    "subprocess",
    "os.environ",
    "open(",
    "hash(",
)

FORBIDDEN_ACCOUNTING_TOKENS = (
    "apply_fill",
    "create_portfolio",
    "value_portfolio",
    "PortfolioState",
)

FORBIDDEN_RISK_ENGINE_TOKENS = (
    "evaluate_order",
    "RiskLimits",
    "RiskReason",
    "risk_prices",
    "estimated_fee",
)

FORBIDDEN_LIVE_TOKENS = (
    "place_order",
    "cancel_order",
    "broker",
    "api_key",
    "credentials",
    "websocket",
    "http://",
    "https://",
)


def production_sources() -> list[Path]:
    return sorted(EXECUTION_SOURCE.rglob("*.py"))


def test_production_sources_exist() -> None:
    sources = production_sources()
    assert PAPER_SOURCE / "execute.py" in sources
    assert PAPER_SOURCE / "__init__.py" in sources
    assert EXECUTION_SOURCE / "__init__.py" in sources


@pytest.mark.parametrize("token", FORBIDDEN_EFFECT_TOKENS)
def test_production_source_has_no_implicit_effect(token: str) -> None:
    for path in production_sources():
        text = path.read_text(encoding="utf-8")
        assert token not in text, f"{token!r} found in {path}"


@pytest.mark.parametrize("token", FORBIDDEN_ACCOUNTING_TOKENS)
def test_production_source_does_not_touch_portfolio_accounting(token: str) -> None:
    for path in production_sources():
        text = path.read_text(encoding="utf-8")
        assert token not in text, f"{token!r} found in {path}"


@pytest.mark.parametrize("token", FORBIDDEN_RISK_ENGINE_TOKENS)
def test_production_source_does_not_recreate_the_risk_engine(token: str) -> None:
    for path in production_sources():
        text = path.read_text(encoding="utf-8")
        assert token not in text, f"{token!r} found in {path}"


@pytest.mark.parametrize("token", FORBIDDEN_LIVE_TOKENS)
def test_production_source_has_no_live_trading_surface(token: str) -> None:
    for path in production_sources():
        text = path.read_text(encoding="utf-8")
        assert token not in text, f"{token!r} found in {path}"


def test_parent_package_marker_stays_minimal() -> None:
    text = (EXECUTION_SOURCE / "__init__.py").read_text(encoding="utf-8")
    assert len(text.splitlines()) <= 2
    assert "import" not in text
