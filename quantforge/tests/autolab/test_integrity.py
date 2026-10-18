from pathlib import Path
from types import SimpleNamespace

from quantforge.autolab.git import GitAdapter


def test_known_untracked_artifact_is_allowed(tmp_path: Path) -> None:
    outputs = {
        "rev-parse HEAD": "abc\n",
        "status --porcelain --untracked-files=all --ignored": "?? audit.json\n",
    }

    def runner(command, cwd, **kwargs):
        key = " ".join(command[1:])
        return SimpleNamespace(returncode=0, stdout=outputs.get(key, ""), stderr="")

    integrity = GitAdapter(tmp_path, runner).integrity(tmp_path, "abc", {"audit.json"})
    assert integrity.valid


def test_unauthorized_untracked_is_invalid(tmp_path: Path) -> None:
    def runner(command, cwd, **kwargs):
        key = " ".join(command[1:])
        return SimpleNamespace(
            returncode=0,
            stdout=("abc\n" if key == "rev-parse HEAD" else "?? bad.py\n"),
            stderr="",
        )

    assert not GitAdapter(tmp_path, runner).integrity(tmp_path, "abc").valid


def test_porcelain_modification_statuses_are_classified() -> None:
    from quantforge.autolab.git import GitAdapter

    statuses = {
        "rev-parse HEAD": "abc\n",
        "status --porcelain --untracked-files=all --ignored": " M file.txt\n",
    }

    def runner(command, cwd, **kwargs):
        return SimpleNamespace(
            returncode=0, stdout=statuses.get(" ".join(command[1:]), ""), stderr=""
        )

    result = GitAdapter(Path("."), runner).integrity(".", "abc")
    assert result.tracked_modified and not result.staged_modified and not result.valid


def test_porcelain_staged_status_is_classified() -> None:
    from quantforge.autolab.git import GitAdapter

    statuses = {
        "rev-parse HEAD": "abc\n",
        "status --porcelain --untracked-files=all --ignored": "M  file.txt\n",
    }

    def runner(command, cwd, **kwargs):
        return SimpleNamespace(
            returncode=0, stdout=statuses.get(" ".join(command[1:]), ""), stderr=""
        )

    result = GitAdapter(Path("."), runner).integrity(".", "abc")
    assert result.staged_modified and not result.tracked_modified and not result.valid


def test_ignored_untracked_status_is_unauthorized() -> None:
    statuses = {
        "rev-parse HEAD": "abc\n",
        "status --porcelain --untracked-files=all --ignored": "!! .env\n",
    }

    def runner(command, cwd, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=statuses.get(" ".join(command[1:]), ""),
            stderr="",
        )

    result = GitAdapter(Path("."), runner).integrity(".", "abc")
    assert result.unauthorized_untracked == (".env",)
    assert not result.valid
