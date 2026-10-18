"""Git metadata must not share a managed worker's blocking control input."""

import os
import subprocess
import sys
from pathlib import Path


def test_context_lookup_finishes_with_a_blocking_stdin_control_reader() -> None:
    executable = sys.executable
    environment = dict(os.environ)
    if os.name == "nt":
        executable = getattr(sys, "_base_executable", None)
        assert isinstance(executable, str) and executable
        environment["__PYVENV_LAUNCHER__"] = sys.executable
    command = [
        executable,
        "-c",
        (
            "import os,threading; "
            "threading.Thread(target=lambda:os.read(0,1),daemon=True).start(); "
            "from quantforge.product.context import code_context; "
            "print(code_context()['git_commit'],flush=True)"
        ),
    ]
    child = subprocess.Popen(
        command,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=Path(__file__).resolve().parents[2],
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        child.wait(timeout=15)
        assert child.returncode == 0
        assert child.stdout is not None
        assert len(child.stdout.read().strip()) == 40
    finally:
        if child.stdin is not None:
            child.stdin.close()
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        for stream in (child.stdout, child.stderr):
            if stream is not None:
                stream.close()
