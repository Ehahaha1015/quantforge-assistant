from types import SimpleNamespace

from quantforge.autolab.ci import run_ci


def test_ci_aggregates_commands() -> None:
    def runner(command, **kwargs):
        return SimpleNamespace(
            returncode=0 if command[0] != "bad" else 1, stdout="", stderr=""
        )

    result = run_ci(".", runner, (("ok", ("ok",)), ("bad", ("bad",))))
    assert not result.passed
    assert [c.name for c in result.commands] == ["ok", "bad"]
