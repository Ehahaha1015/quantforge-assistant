from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class LauncherType(StrEnum):
    NATIVE = "native"
    POWERSHELL = "powershell"
    CMD = "cmd"
    NODE = "node"
    UNSUPPORTED = "unsupported"


class LauncherError(RuntimeError):
    """A reviewer executable cannot be resolved safely."""


@dataclass(frozen=True)
class ResolvedLauncher:
    launcher_type: LauncherType
    executable: str
    prefix: tuple[str, ...] = ()

    def argv(self, args: list[str]) -> list[str]:
        if self.launcher_type == LauncherType.CMD:
            command = "call " + " ".join(_cmd_quote(value) for value in (self.executable, *args))
            return [*self.prefix, command]
        return [*self.prefix, self.executable, *args]


def _cmd_quote(value: str) -> str:
    """Quote a cmd argument while rejecting expansion syntax."""
    if any(
        token in value for token in ('%', '"', '&', '|', '<', '>', '^')
    ) or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise LauncherError("unsafe CMD metacharacter in reviewer argument")
    return f'"{value}"'


def _which(candidates: tuple[str, ...]) -> str | None:
    for candidate in candidates:
        # ``shutil.which`` deliberately honours PATHEXT on Windows.  A
        # PowerShell shim is not normally listed there, so check PATH entries
        # explicitly as well.  This keeps resolution deterministic while still
        # allowing the preferred ``dsh.ps1`` route to be selected.
        candidate_path = Path(candidate)
        if candidate_path.is_absolute() and candidate_path.is_file():
            return str(candidate_path.resolve())
        if not candidate_path.is_absolute():
            directories = os.environ.get("PATH", "").split(os.pathsep)
            extensions: tuple[str, ...] = ("",)
            if os.name == "nt" and not candidate_path.suffix:
                extensions = tuple(
                    extension
                    for extension in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";")
                    if extension
                )
            for directory in directories:
                if not directory:
                    continue
                for extension in extensions:
                    path = Path(directory) / f"{candidate}{extension}"
                    if path.is_file():
                        return str(path.resolve())
    return None


def _resolve_pwsh() -> str:
    found = _which(("pwsh.exe",))
    if found is None:
        raise LauncherError("PowerShell pwsh.exe is required for .ps1 reviewers")
    return found


def resolve_launcher(executable: str) -> ResolvedLauncher:
    """Resolve a reviewer command using deterministic Windows-safe precedence."""
    supplied = Path(executable)
    suffix = supplied.suffix.lower()
    if suffix in {".ps1", ".cmd", ".bat", ".exe", ".js"} and supplied.is_absolute():
        path = supplied
    else:
        name = executable
        if os.name == "nt" and name.lower() in {"dsh", "dsh.exe"}:
            # npm exposes dsh.ps1 and dsh.cmd; prefer the proven PowerShell route.
            found = _which(("dsh.ps1", "dsh.cmd", "dsh.exe", "dsh"))
        elif os.name == "nt" and name.lower() in {"codex", "codex.exe"}:
            # Codex is required to be a native executable; never fall back to
            # an npm CMD shim.
            found = _which(("codex.exe",))
        else:
            found = _which((name,))
        if found is None:
            raise LauncherError(f"reviewer executable not found: {executable}")
        path = Path(found)
        suffix = path.suffix.lower()

    if not path.exists():
        raise LauncherError(f"reviewer executable not found: {path}")
    resolved = str(path.resolve())
    if suffix == ".ps1":
        return ResolvedLauncher(
            LauncherType.POWERSHELL,
            resolved,
            (_resolve_pwsh(), "-NoProfile", "-NonInteractive", "-File"),
        )
    if suffix in {".cmd", ".bat"}:
        configured = os.environ.get("COMSPEC", "")
        comspec = (
            str(Path(configured).resolve())
            if Path(configured).is_absolute() and Path(configured).is_file()
            else _which(("cmd.exe",))
        )
        if not comspec:
            raise LauncherError("cmd.exe is required for .cmd/.bat reviewers")
        return ResolvedLauncher(LauncherType.CMD, resolved, (str(comspec), "/d", "/s", "/c"))
    if suffix == ".js":
        node = _which(("node.exe",))
        if node is None:
            raise LauncherError("node.exe is required for JavaScript reviewers")
        return ResolvedLauncher(LauncherType.NODE, resolved, (node,))
    if suffix and suffix not in {".exe", ".com"}:
        raise LauncherError(f"unsupported reviewer launcher: {path}")
    return ResolvedLauncher(LauncherType.NATIVE, resolved)
