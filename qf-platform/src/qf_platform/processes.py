"""Launch the actual interpreter while retaining the active Windows venv."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence

from .storage import PlatformError


def python_process(arguments: Sequence[str]) -> tuple[list[str], dict[str, str]]:
    environment = dict(os.environ)
    executable = sys.executable
    if os.name == "nt":
        # The venv redirector owns another PID and can outlive its launcher.
        base = getattr(sys, "_base_executable", None)
        if not isinstance(base, str) or not base:
            raise PlatformError("NATIVE_PYTHON_INTERPRETER_UNAVAILABLE")
        executable = base
        environment["__PYVENV_LAUNCHER__"] = sys.executable
        environment.pop("PYTHONEXECUTABLE", None)
        environment.pop("PYTHONHOME", None)
    return [executable, *arguments], environment
