from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from ..models import TransportStatus, TransportTelemetry
from ..transport import LauncherType, ResolvedLauncher, resolve_launcher


class ProcessAdapter:
    def __init__(
        self,
        executable: str,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.executable = executable
        self.runner = runner
        self.last_telemetry: TransportTelemetry | None = None

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    def _record(
        self,
        launcher: ResolvedLauncher,
        started: float,
        started_at: str,
        ended_at: str,
        stdout: str = "",
        stderr: str = "",
        exit_code: int | None = None,
        status: TransportStatus = TransportStatus.SUCCESS,
    ) -> None:
        self.last_telemetry = TransportTelemetry(
            launcher_type=launcher.launcher_type.value,
            resolved_executable=launcher.executable,
            start_time=started_at,
            # communicate() only exposes completed streams; do not claim the
            # process end as the first-byte timestamp.
            first_output_time=None,
            end_time=ended_at,
            duration_seconds=round(time.monotonic() - started, 6),
            exit_code=exit_code,
            transport_status=status.value,
            timeout_classification=(
                status.value
                if status in {TransportStatus.READINESS_TIMEOUT, TransportStatus.HARD_TIMEOUT}
                else ""
            ),
            stdout_bytes=len(stdout.encode("utf-8")),
            stderr_bytes=len(stderr.encode("utf-8")),
        )

    def finalize_telemetry(self, reviewer: str, status: TransportStatus) -> TransportTelemetry | None:
        if self.last_telemetry is None:
            return None
        self.last_telemetry = replace(
            self.last_telemetry,
            reviewer=reviewer,
            transport_status=status.value,
            timeout_classification=(
                status.value
                if status in {TransportStatus.READINESS_TIMEOUT, TransportStatus.HARD_TIMEOUT}
                else self.last_telemetry.timeout_classification
            ),
        )
        return self.last_telemetry

    def invoke(
        self, args: list[str], cwd: str, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        self.last_telemetry = None
        launcher = resolve_launcher(self.executable)
        command = launcher.argv(args)
        started = time.monotonic()
        started_at = self._now()
        if self.runner is not subprocess.run:
            completed = self.runner(
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=os.environ.copy(),
                timeout=timeout,
                check=False,
            )
            stdout = self._text(getattr(completed, "stdout", ""))
            stderr = self._text(getattr(completed, "stderr", ""))
            self._record(
                launcher,
                started,
                started_at,
                self._now(),
                stdout,
                stderr,
                getattr(completed, "returncode", None),
                TransportStatus.PROCESS_ERROR
                if getattr(completed, "returncode", 0) not in (0, None)
                else TransportStatus.SUCCESS,
            )
            return completed
        popen_command: str | list[str] = command
        if launcher.launcher_type == LauncherType.CMD:
            popen_command = subprocess.list2cmdline(list(launcher.prefix)) + " " + command[-1]
        try:
            process = subprocess.Popen(
                popen_command,
                cwd=cwd,
                env=os.environ.copy(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=(os.name != "nt"),
            )
        except OSError:
            self._record(
                launcher, started, started_at, self._now(),
                exit_code=None, status=TransportStatus.SPAWN_ERROR,
            )
            raise
        job = self._create_job(process)
        try:
            stdout, stderr = process.communicate(input="", timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            self._terminate_tree(process, job)
            try:
                stdout, stderr = process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                stdout = self._text(getattr(exc, "output", ""))
                stderr = self._text(getattr(exc, "stderr", ""))
            stdout = self._text(stdout)
            stderr = self._text(stderr)
            self._record(
                launcher, started, started_at, self._now(), stdout, stderr,
                process.returncode, TransportStatus.HARD_TIMEOUT,
            )
            raise subprocess.TimeoutExpired(
                command, timeout, output=stdout, stderr=stderr
            )
        except BaseException:
            self._terminate_tree(process, job)
            raise
        finally:
            self._close_job(job)
        stdout = self._text(stdout)
        stderr = self._text(stderr)
        status = (
            TransportStatus.PROCESS_ERROR
            if process.returncode not in (0, None)
            else TransportStatus.SUCCESS
        )
        self._record(launcher, started, started_at, self._now(), stdout, stderr, process.returncode, status)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)

    @staticmethod
    def _close_pipes(process: subprocess.Popen[str]) -> None:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    @staticmethod
    def _create_job(process: subprocess.Popen[str]) -> Any:
        if sys.platform != "win32":
            return None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        class BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("Priority", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [("values", ctypes.c_ulonglong * 6)]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimit),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        process_handle = getattr(process, "_handle", None)
        if process_handle is None or not kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(info), ctypes.sizeof(info)
        ) or not kernel32.AssignProcessToJobObject(
            job, wintypes.HANDLE(int(process_handle))
        ):
            kernel32.CloseHandle(job)
            return None
        return job

    @staticmethod
    def _close_job(job: Any) -> None:
        if sys.platform == "win32" and job:
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(job)

    @staticmethod
    def _terminate_tree(process: subprocess.Popen[str], job: Any = None) -> None:
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    check=False,
                    timeout=2,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        else:
            try:
                os.killpg(process.pid, getattr(signal, "SIGKILL", signal.SIGTERM))  # type: ignore[attr-defined]
            except (OSError, ProcessLookupError):
                try:
                    process.kill()
                except OSError:
                    pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                # Do not synchronously close pipes here: a descendant may
                # still own a duplicated handle.  The bounded timeout path
                # must return without waiting for that descendant.
                pass

    def preflight(self) -> None:
        self.last_telemetry = None
        resolve_launcher(self.executable)

    def launcher(self):
        """Return the resolved launcher for diagnostics and preflight checks."""
        return resolve_launcher(self.executable)
