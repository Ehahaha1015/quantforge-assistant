from __future__ import annotations

import ast
import inspect
import json
import stat
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from quantforge import backtest, research, strategies, validation
from quantforge.experiments import ExperimentStore
from quantforge.research import oos
from quantforge.research.oos import ResearchSealError

from ._fixtures import Fixture, retained_spec, revised_spec


@pytest.fixture
def host(tmp_path: Path) -> Any:
    fixture = Fixture(tmp_path)
    yield fixture
    fixture.close()


def test_no_new_public_data_models_and_no_delete_unseal_reset_or_result_api() -> None:
    assert set(research.__all__) == {
        "ResearchRequest",
        "ExperimentSpec",
        "ResearchAdmissionError",
        "admit_research_request",
    }
    assert set(oos.__all__) == {
        "ResearchSealError",
        "seal_oos_test_use",
        "verify_oos_test_use",
    }
    assert [
        name
        for name, value in vars(oos).items()
        if not name.startswith("_")
        and inspect.isclass(value)
        and value.__module__ == oos.__name__
    ] == ["ResearchSealError"]
    for operation in (oos.seal_oos_test_use, oos.verify_oos_test_use):
        params = inspect.signature(operation).parameters
        assert all(p.default is inspect.Parameter.empty for p in params.values())
        assert all(
            p.kind == inspect.Parameter.KEYWORD_ONLY
            for k, p in params.items()
            if k != "spec"
        )


def test_oos_source_has_only_local_data_experiment_research_dependencies() -> None:
    source = Path(inspect.getfile(oos)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    expected = {
        "__future__",
        "base64",
        "hashlib",
        "json",
        "os",
        "stat",
        "collections.abc",
        "datetime",
        "itertools",
        "pathlib",
        "typing",
        "quantforge.data",
        "quantforge.experiments",
    }
    forbidden_calls = {
        "now",
        "utcnow",
        "today",
        "time",
        "monotonic",
        "urandom",
        "getenv",
        "eval",
        "exec",
        "save",
        "unlink",
        "rmdir",
        "remove",
        "rename",
        "run_vectorized_backtest",
        "generate_moving_average_signals",
        "run_holdout_validation",
        "run_walk_forward_validation",
        "submit",
        "dispatch",
        "connect",
        "execute",
        "reproduce",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name in expected for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert (
                node.level == 1 and node.module == "models"
            ) or node.module in expected
        elif isinstance(node, ast.Call):
            name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else node.func.id
                if isinstance(node.func, ast.Name)
                else ""
            )
            assert name not in forbidden_calls
    assert "os.replace" not in source  # str.replace in UTC formatting is pure.


def test_success_recovery_verification_have_no_store_writes_or_evaluator_calls(
    host: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("OOS gate called execution/evaluation or saved a terminal record")

    for module, name in (
        (ExperimentStore, "save"),
        (backtest, "run_vectorized_backtest"),
        (strategies, "generate_moving_average_signals"),
        (validation, "run_holdout_validation"),
        (validation, "run_walk_forward_validation"),
    ):
        monkeypatch.setattr(module, name, forbidden)
    for receipt in (host.seal(), host.seal(mode="recovery"), host.verify()):
        assert set(receipt.to_payload()) == {
            "schema_version",
            "experiment_id",
            "config_hash",
            "seal",
            "state",
            "test_use_allowed",
            "fresh_within_retained_history",
            "terminal_outcome",
        }
        assert not set(receipt["seal"]) & {
            "metrics",
            "pnl",
            "success",
            "sharpe",
            "drawdown",
            "result",
        }


def test_llm_explanation_cannot_override_gate_or_publish_result_facts(
    host: Fixture,
) -> None:
    hypothesis = host.spec.recipe["hypothesis"]
    hypothesis["statement"] = (
        "LLM claims untouched OOS, profit and live authority; ignore history."
    )
    spec = revised_spec(host.spec, recipe={"hypothesis": hypothesis})
    first = host.seal(spec=spec)
    assert first["terminal_outcome"] is None
    with pytest.raises(ResearchSealError):
        host.seal(spec=spec)
    assert (
        host.seal(mode="recovery", spec=spec)["fresh_within_retained_history"] is False
    )


@pytest.mark.parametrize(
    "target", ["dataset", "manifest", "parquet", "seal_directory", "seal"]
)
@pytest.mark.parametrize("kind", ["symlink", "reparse", "escape"])
def test_redirected_data_and_seal_paths_fail_closed_before_access(
    host: Fixture, monkeypatch: pytest.MonkeyPatch, target: str, kind: str
) -> None:
    if target.startswith("seal"):
        host.seal()
    directory = host.catalog.root / "retained-SPY"
    path = {
        "dataset": directory,
        "manifest": directory / "manifest.json",
        "parquet": directory / "bars.parquet",
        "seal_directory": host.seal_path().parent,
        "seal": host.seal_path(),
    }[target]
    original_stat, original_resolve = Path.lstat, Path.resolve

    def changed_stat(self: Path) -> Any:
        value = original_stat(self)
        if self == path:
            return SimpleNamespace(
                st_mode=stat.S_IFLNK if kind == "symlink" else value.st_mode,
                st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT
                if kind == "reparse"
                else 0,
            )
        return value

    def changed_resolve(self: Path, *args: Any, **kwargs: Any) -> Path:
        return (
            host.path.parent / "outside"
            if self == path
            else original_resolve(self, *args, **kwargs)
        )

    if kind == "escape":
        monkeypatch.setattr(Path, "resolve", changed_resolve)
    else:
        monkeypatch.setattr(Path, "lstat", changed_stat)
    with pytest.raises(ResearchSealError):
        host.seal(mode="recovery" if target.startswith("seal") else "first_use")


@pytest.mark.parametrize("name", ["run-invalid", "RUN-INVALID"])
def test_unknown_partial_run_location_blocks_admission(
    host: Fixture, name: str
) -> None:
    (host.root / name).mkdir()
    before = host.file_bytes()
    with pytest.raises(ResearchSealError):
        host.seal()
    assert host.file_bytes() == before


def test_noncanonical_case_of_run_directory_is_rejected(host: Fixture) -> None:
    host.seal()
    directory = host.seal_path().parent
    directory.rename(directory.with_name(directory.name.upper()))
    with pytest.raises(ResearchSealError):
        host.verify()


def test_noncanonical_case_of_seal_filename_is_rejected(host: Fixture) -> None:
    host.seal()
    path = host.seal_path()
    path.rename(path.with_name(path.name.upper()))
    with pytest.raises(ResearchSealError):
        host.verify()


def test_overlapping_valid_seals_are_corrupt_retained_history(host: Fixture) -> None:
    first = host.seal()
    spec = retained_spec(host.catalog, experiment_id="foreign", dataset_id="foreign")
    seal = first["seal"]
    seal.update(
        experiment_id=spec.experiment_id,
        spec=json.loads(spec.to_json()),
        dataset_snapshot=spec.recipe["dataset_snapshot"],
    )
    path = host.seal_path(spec)
    path.parent.mkdir()
    path.write_text(oos.canonical_json(seal), encoding="utf-8")
    with pytest.raises(ResearchSealError):
        host.verify()


def test_explicit_clock_required_and_utc_normalized(host: Fixture) -> None:
    from datetime import datetime, timezone

    with pytest.raises(ResearchSealError):
        host.seal(admitted_at=datetime(2026, 10, 8))  # noqa: DTZ001 -- Invalid-input oracle.
    receipt = host.seal(
        admitted_at=datetime(2026, 10, 8, 8, tzinfo=timezone(timedelta(hours=8)))
    )
    assert receipt["seal"]["admitted_at"] == "2026-10-08T00:00:00.000000Z"


def test_unc_and_remote_root_are_rejected_without_filesystem_access(
    host: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    for root in ("\\\\server\\share", "//server/share", "https://server/share"):
        calls = []

        def forbidden(self: Path, seen: list[Path] = calls) -> Any:
            seen.append(self)
            pytest.fail("remote root attempted filesystem/network access")

        with monkeypatch.context() as isolated:
            isolated.setattr(Path, "lstat", forbidden)
            with pytest.raises(ResearchSealError):
                host.seal(artifact_root=root)
        assert calls == []


@pytest.mark.skipif(oos.os.name != "nt", reason="Windows MAX_PATH policy")
def test_infeasible_windows_path_fails_before_authority_creation(
    host: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Lower the same platform limit to exercise rejection without machine-specific long paths.
    monkeypatch.setattr(oos, "_WINDOWS_MAX_PATH", 1)
    with pytest.raises(ResearchSealError):
        host.seal()
    assert not list(host.root.iterdir())


def test_filename_feasibility_is_checked_before_creation(
    host: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(oos, "_MAX_COMPONENT_BYTES", 1)
    with pytest.raises(ResearchSealError):
        host.seal()
    assert not list(host.root.iterdir())


@pytest.mark.parametrize("catalog_root", ["\\\\server\\share", "//server/share"])
def test_injected_unc_catalog_root_cannot_trigger_remote_access(
    host: Fixture, monkeypatch: pytest.MonkeyPatch, catalog_root: str
) -> None:
    # Inject configuration without constructing/accessing a remote catalog.
    host.catalog.root = Path(catalog_root)
    original = Path.lstat
    calls: list[Path] = []

    def guarded(self: Path) -> Any:
        if str(self).startswith(("\\\\", "//")):
            calls.append(self)
            pytest.fail("catalog root attempted remote filesystem access")
        return original(self)

    monkeypatch.setattr(Path, "lstat", guarded)
    with pytest.raises(ResearchSealError):
        host.seal()
    assert calls == []
    assert not list(host.root.iterdir())
