from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import quantforge.live as live_package

LIVE_SOURCE = Path(__file__).resolve().parents[2] / "src" / "quantforge" / "live"

# Coarse guards required by the installed contract. The real guarantee is the
# value design plus the import-boundary check below; a string scan alone would
# not prove more than it proves.
FORBIDDEN_TOKENS = (
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
    "model_construct",
    "identify",
    "apply_fill",
    "create_portfolio",
    "value_portfolio",
    "PortfolioState",
    "Fill",
    "evaluate_order",
    "place_order",
    "cancel_order",
    "replace_order",
    "api_key",
    "secret",
    "token",
    "http://",
    "https://",
    "paper1-",
    "quantforge.execution",
)


def production_sources() -> list[Path]:
    return sorted(LIVE_SOURCE.rglob("*.py"))


def test_production_sources_exist() -> None:
    sources = production_sources()
    assert LIVE_SOURCE / "__init__.py" in sources
    assert LIVE_SOURCE / "authorization.py" in sources


@pytest.mark.parametrize("token", FORBIDDEN_TOKENS)
def test_production_source_avoids_forbidden_token(token: str) -> None:
    for path in production_sources():
        text = path.read_text(encoding="utf-8")
        assert token not in text, f"{token!r} found in {path}"


def test_live_import_pulls_in_no_execution_or_autolab_package() -> None:
    code = (
        "import sys\n"
        "import quantforge.live\n"
        "leaked = [name for name in ('quantforge.execution', 'quantforge.autolab')"
        " if name in sys.modules]\n"
        "print(','.join(leaked))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    assert completed.stdout.strip() == ""


def test_live_module_namespace_holds_no_accounting_api() -> None:
    names = {name for name in vars(live_package.authorization) if not name.startswith("_")}
    forbidden = {"apply_fill", "create_portfolio", "value_portfolio", "PortfolioState"}
    assert not names & forbidden
    assert "Fill" not in names


def test_module_documents_the_human_provenance_limit() -> None:
    docstring = live_package.authorization.__doc__ or ""
    assert docstring.strip()
    assert "cannot prove" in docstring
    assert "declaration" in docstring


def test_public_names_have_no_execution_methods() -> None:
    for name in ("submit", "execute", "place_order", "cancel_order", "replace_order"):
        assert not hasattr(live_package.LiveExecutionPermission, name)
