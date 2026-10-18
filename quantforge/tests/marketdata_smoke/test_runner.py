"""Focused tests for the authenticated market observation smoke runner."""

from __future__ import annotations

import ast
import json
import linecache
import logging
import traceback
import warnings
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from _marketdata_fixtures import make_artifact

from quantforge import marketdata_smoke as smoke
from quantforge.marketdata import (
    PriceArgumentError,
    PriceCoverageError,
    PriceEvidenceError,
    PriceFutureObservationError,
    PriceObservationRequest,
    PriceProvenanceMismatchError,
    PriceSourceError,
    PriceStaleObservationError,
    PriceTimestampError,
    VerifiedPriceObservation,
)
from quantforge.marketdata.canonical import _observation_id
from quantforge.marketdata_smoke import _parse_dispatch_timestamp, run_smoke
from quantforge.provenance import FreshRiskProvenance, RiskPriceFact
from tests.provenance._fixtures import EVALUATED_AT

_DISPATCH = EVALUATED_AT
_SOURCE_ID = "alpaca-iex-latest-trade-v1"


def _prices(
    *,
    aapl: Decimal = Decimal("178.26"),
    msft: Decimal = Decimal("512.34"),
    spy: Decimal = Decimal("641.02"),
) -> tuple[RiskPriceFact, ...]:
    return (
        RiskPriceFact(symbol="AAPL", price=aapl),
        RiskPriceFact(symbol="MSFT", price=msft),
        RiskPriceFact(symbol="SPY", price=spy),
    )


def _observation(
    *,
    dispatch_timestamp: datetime = _DISPATCH,
    observed_at: datetime | None = None,
    maximum_age: timedelta = timedelta(seconds=15),
    risk_prices: tuple[RiskPriceFact, ...] | None = None,
) -> VerifiedPriceObservation:
    observed = (
        dispatch_timestamp - timedelta(seconds=1)
        if observed_at is None
        else observed_at
    )
    source_observation_id = "synthetic-observation-1"
    facts = _prices() if risk_prices is None else risk_prices
    return VerifiedPriceObservation(
        schema="marketdata-observation-v1",
        observation_id=_observation_id(
            market_source=_SOURCE_ID,
            source_observation_id=source_observation_id,
            observed_at=observed,
            maximum_age=maximum_age,
            risk_prices=facts,
        ),
        market_source=_SOURCE_ID,
        source_observation_id=source_observation_id,
        observed_at=observed,
        maximum_age=maximum_age,
        risk_prices=facts,
    )


class _SyntheticSource:
    source_id = _SOURCE_ID

    def __init__(self, observation: VerifiedPriceObservation) -> None:
        self.observation = observation
        self.calls = 0
        self.observe_calls = 0
        self.capture_calls = 0
        self.requests: list[PriceObservationRequest] = []

    def _capture_smoke_observation(
        self,
        request: PriceObservationRequest,
        *,
        dispatch_timestamp: datetime,
    ) -> SimpleNamespace:
        self.calls += 1
        self.capture_calls += 1
        self.requests.append(request)
        age_microseconds = int(
            (dispatch_timestamp - self.observation.observed_at).total_seconds()
            * 1_000_000
        )
        if age_microseconds < 0:
            category = "FUTURE"
        elif age_microseconds <= 15_000_000:
            category = "FRESH"
        elif age_microseconds == 15_000_001:
            category = "STALE_THRESHOLD"
        else:
            category = "STALE"
        return SimpleNamespace(
            observation=self.observation,
            diagnostic=SimpleNamespace(
                aggregate_age_category=category,
                per_symbol_relation=tuple(
                    (symbol, category) for symbol in smoke.FROZEN_SYMBOLS
                ),
            ),
        )

    def observe(self, request: PriceObservationRequest) -> VerifiedPriceObservation:
        self.observe_calls += 1
        raise AssertionError("observe() must not be called by the smoke runner")


class _FailingSource:
    source_id = _SOURCE_ID

    def __init__(self) -> None:
        self.calls = 0

    def _capture_smoke_observation(
        self,
        request: PriceObservationRequest,
        *,
        dispatch_timestamp: datetime,
    ) -> object:
        self.calls += 1
        raise PriceSourceError("source failure sentinel")


def _provenance(
    prices: dict[str, Decimal] | None = None,
) -> FreshRiskProvenance:
    selected = (
        {
            "AAPL": Decimal("178.26"),
            "MSFT": Decimal("512.34"),
            "SPY": Decimal("641.02"),
        }
        if prices is None
        else prices
    )
    from quantforge.domain import Position
    from tests.provenance._fixtures import make_order, make_portfolio

    return make_artifact(
        order=make_order(symbol="SPY"),
        portfolio=make_portfolio(
            positions=(
                Position(
                    symbol="AAPL", quantity=Decimal(2), average_price=Decimal(100)
                ),
                Position(symbol="MSFT", quantity=Decimal(2), average_price=Decimal(50)),
            )
        ),
        prices=selected,
        evaluation_timestamp=_DISPATCH,
    )


def test_run_smoke_uses_frozen_symbols_once_and_renders_safe_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation = _observation()
    source = _SyntheticSource(observation)
    monkeypatch.setattr(smoke, "build_alpaca_iex_source", lambda: source)

    result = run_smoke(dispatch_timestamp=_DISPATCH)

    assert source.calls == 1
    request = source.requests[0]
    assert request.schema == "marketdata-request-v1"
    assert request.symbols == smoke.FROZEN_SYMBOLS

    expected = {
        "status": "SUCCESS",
        "dispatch_timestamp": "2030-01-01T12:02:00.000000Z",
        "symbols": ["AAPL", "MSFT", "SPY"],
        "prices": {"AAPL": "178.26", "MSFT": "512.34", "SPY": "641.02"},
        "observed_at": "2030-01-01T12:01:59.000000Z",
        "source_id": "alpaca-iex-latest-trade-v1",
        "market_source": "alpaca-iex-latest-trade-v1",
        "source_observation_id": "synthetic-observation-1",
        "observation_id": (
            "marketdata-obs-v1-"
            "c99940b0acbb7a44d3355ab48056b25dd98849102332db4f15cd8ffc8553fc85"
        ),
        "maximum_age": "15000000",
        "freshness_success": True,
        "evidence_id": (
            "marketdata-price-v1-"
            "15a30e5e896e4838572a1d50f3a5f9bcf6c5c10262386752f2c269d80960220b"
        ),
        "provenance_id": (
            "riskprov1-de6c515b1c5b5dde4233f78bd1cdc539608bc3e69244c12a691dfff274cb8f42"
        ),
    }
    expected_types = {
        "status": str,
        "dispatch_timestamp": str,
        "symbols": list,
        "prices": dict,
        "observed_at": str,
        "source_id": str,
        "market_source": str,
        "source_observation_id": str,
        "observation_id": str,
        "maximum_age": str,
        "freshness_success": bool,
        "evidence_id": str,
        "provenance_id": str,
    }
    assert type(result) is dict
    assert result == expected
    assert {key: type(value) for key, value in result.items()} == expected_types
    assert type(result["symbols"]) is list
    assert all(type(symbol) is str for symbol in result["symbols"])
    assert type(result["prices"]) is dict
    assert all(
        type(symbol) is str and type(price) is str
        for symbol, price in result["prices"].items()
    )
    rendered = json.dumps(result, sort_keys=True)
    for forbidden in (
        "key_id",
        "secret_key",
        "authorization",
        "SecretMaterial",
        "CredentialLease",
        "CredentialBoundary",
        "credential_name",
        "lease_material",
        "api_key",
    ):
        assert forbidden.casefold() not in rendered.casefold()


def test_captured_observation_drives_provenance_and_one_local_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation = _observation()
    source = _SyntheticSource(observation)
    captured: dict[str, object] = {}
    monkeypatch.setattr(smoke, "build_alpaca_iex_source", lambda: source)
    original = smoke.produce_price_freshness_evidence

    def recording_producer(**kwargs: object) -> object:
        provenance = kwargs["provenance"]
        replay = kwargs["source"]
        captured["provenance"] = provenance
        captured["replay"] = replay
        assert isinstance(replay, smoke._ReplaySource)
        assert replay._observation is observation
        return original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(smoke, "produce_price_freshness_evidence", recording_producer)

    result = run_smoke(dispatch_timestamp=_DISPATCH)

    provenance = captured["provenance"]
    replay = captured["replay"]
    assert isinstance(provenance, FreshRiskProvenance)
    assert provenance.risk_prices == observation.risk_prices
    assert isinstance(replay, smoke._ReplaySource)
    assert replay._observation is observation
    assert replay._used is True
    assert source.calls == 1
    assert result["status"] == "SUCCESS"


def test_run_smoke_passes_the_same_timestamp_to_price_producer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _SyntheticSource(_observation())
    observed: dict[str, object] = {}

    monkeypatch.setattr(smoke, "build_alpaca_iex_source", lambda: source)
    original = smoke.produce_price_freshness_evidence

    def recording_producer(**kwargs: object) -> object:
        observed["dispatch_timestamp"] = kwargs["dispatch_timestamp"]
        return original(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(smoke, "produce_price_freshness_evidence", recording_producer)

    run_smoke(dispatch_timestamp=_DISPATCH)

    assert observed["dispatch_timestamp"] is _DISPATCH


def test_price_producer_still_rejects_observation_provenance_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observation = _observation()
    source = _SyntheticSource(observation)
    monkeypatch.setattr(smoke, "build_alpaca_iex_source", lambda: source)
    monkeypatch.setattr(
        smoke,
        "_build_provenance",
        lambda *, dispatch_timestamp, observation: _provenance(
            prices={"AAPL": Decimal(1), "MSFT": Decimal(2), "SPY": Decimal(3)}
        ),
    )

    with pytest.raises(PriceProvenanceMismatchError):
        run_smoke(dispatch_timestamp=_DISPATCH)

    assert source.calls == 1


def test_existing_freshness_semantics_remain_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _SyntheticSource(
        _observation(
            observed_at=_DISPATCH - timedelta(seconds=2),
            maximum_age=timedelta(seconds=1),
        )
    )
    monkeypatch.setattr(smoke, "build_alpaca_iex_source", lambda: source)

    with pytest.raises(PriceStaleObservationError):
        run_smoke(dispatch_timestamp=_DISPATCH)

    assert source.calls == 1


def test_cli_requires_explicit_timestamp() -> None:
    with pytest.raises(SystemExit) as info:
        smoke.main([])

    assert info.value.code == 2


def test_timestamp_parser_requires_canonical_utc() -> None:
    assert _parse_dispatch_timestamp("2030-01-01T12:02:00.000000Z") == _DISPATCH
    with pytest.raises(PriceTimestampError):
        _parse_dispatch_timestamp("2030-01-01T12:02:00+00:00")


def test_cli_renders_sanitized_source_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _FailingSource()
    monkeypatch.setattr(smoke, "build_alpaca_iex_source", lambda: source)

    exit_code = smoke.main(["--dispatch-timestamp", "2030-01-01T12:02:00.000000Z"])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert output == {
        "status": "FAILURE",
        "error": {
            "type": "PriceEvidenceError",
            "message": "market data evidence is invalid",
            "reason": "SOURCE_FAILURE",
        },
    }
    assert source.calls == 1


def _contains_material(text: str, material: tuple[str, ...]) -> bool:
    return any(value and value in text for value in material)


def test_cli_sanitizes_source_constructor_failure_across_all_channels(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    outer_sentinel = "OUTER_SENTINEL_constructor_message"
    chain_sentinel = "CHAIN_SENTINEL_cause_context"
    raw_constructor_material: list[str] = []

    def failing_builder() -> object:
        try:
            try:
                raise LookupError(f"context {chain_sentinel}")
            except LookupError:
                failure = RuntimeError(
                    f"outer {outer_sentinel}", f"argument {outer_sentinel}"
                )
                failure.__cause__ = ValueError(f"explicit cause {chain_sentinel}")
                raise failure
        except Exception as exc:
            cause = exc.__cause__
            context = exc.__context__
            raw_constructor_material.extend(
                (
                    str(exc),
                    repr(exc),
                    repr(exc.args),
                    str(cause),
                    repr(cause),
                    repr(cause.args),
                    str(context),
                    repr(context),
                    repr(context.args),
                    "".join(traceback.format_exception(exc)),
                )
            )
            assert cause is not None
            assert context is not None
            raise

    monkeypatch.setattr(smoke, "build_alpaca_iex_source", failing_builder)
    caplog.clear()
    caplog.set_level(logging.NOTSET)

    # The complete real CLI failure operation is covered by the all-warning policy.
    with warnings.catch_warnings(record=True) as captured_warnings:
        warnings.simplefilter("always")
        exit_code = smoke.main(
            ["--dispatch-timestamp", "2030-01-01T12:02:00.000000Z"]
        )

    captured = capsys.readouterr()
    public_result = json.loads(captured.out)
    expected_failure = {
        "status": "FAILURE",
        "error": {
            "type": "PriceEvidenceError",
            "message": "market data evidence is invalid",
            "reason": "SOURCE_FAILURE",
        },
    }
    assert exit_code == 1
    assert public_result == expected_failure
    assert captured.out == smoke._json_output(expected_failure) + "\n"
    assert captured.err == ""
    assert len(raw_constructor_material) == 10

    channels = [
        json.dumps(public_result, sort_keys=True, separators=(",", ":")),
        captured.out,
        captured.err,
    ]
    for record in caplog.records:
        channels.extend(
            (
                str(record.msg),
                repr(record.args),
                record.getMessage(),
                logging.Formatter().format(record),
            )
        )
        if record.exc_text is not None:
            channels.append(record.exc_text)
        if record.exc_info is not None:
            channels.append("".join(traceback.format_exception(*record.exc_info)))

    for warning in captured_warnings:
        warning_message = str(warning.message)
        warning_rendering = warnings.formatwarning(
            warning.message,
            warning.category,
            warning.filename,
            warning.lineno,
            line=None,
        )
        channels.extend((warning_message, warning_rendering))

    blocked_material = (
        outer_sentinel,
        chain_sentinel,
        *raw_constructor_material,
    )
    for channel in channels:
        assert not _contains_material(channel, blocked_material)


def test_warning_controls_cover_suppression_and_standard_source_lookup() -> None:
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        warnings.warn("suppressed-category-control", DeprecationWarning)

    assert len(captured) == 1
    assert captured[0].category is DeprecationWarning
    assert str(captured[0].message) == "suppressed-category-control"

    filename = "marketdata-smoke-warning-line-control.py"
    line_number = 3
    source_line = "safe source OUTER_SENTINEL CHAIN_SENTINEL\n"
    source_lines = ["# deterministic source fixture\n", "safe prelude\n", source_line]
    source_text = "".join(source_lines)
    previous_entry = linecache.cache.get(filename)
    linecache.cache[filename] = (len(source_text), None, source_lines, filename)
    try:
        rendered = warnings.formatwarning(
            UserWarning("safe warning message"),
            UserWarning,
            filename,
            line_number,
            line=None,
        )
        sentinels = ("OUTER_SENTINEL", "CHAIN_SENTINEL")
        assert "safe warning message" in rendered
        assert "UserWarning" in rendered
        assert filename in rendered
        assert f":{line_number}:" in rendered
        assert source_line.strip() in rendered
        assert _contains_material(rendered, sentinels)
    finally:
        if previous_entry is None:
            linecache.cache.pop(filename, None)
        else:
            linecache.cache[filename] = previous_entry


def test_constructor_price_evidence_error_keeps_observe_stage_mapping(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_with_domain_error() -> object:
        raise PriceCoverageError("synthetic constructor coverage detail")

    monkeypatch.setattr(smoke, "build_alpaca_iex_source", fail_with_domain_error)
    exit_code = smoke.main(
        ["--dispatch-timestamp", "2030-01-01T12:02:00.000000Z"]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert json.loads(captured.out) == {
        "status": "FAILURE",
        "error": {
            "type": "PriceEvidenceError",
            "message": "market data evidence is invalid",
            "reason": "SYMBOL_COVERAGE_INVALID",
        },
    }
    assert "synthetic constructor coverage detail" not in captured.out + captured.err


def test_constructor_base_exception_is_not_caught(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupt_builder() -> object:
        raise KeyboardInterrupt("synthetic interrupt")

    monkeypatch.setattr(smoke, "build_alpaca_iex_source", interrupt_builder)
    with pytest.raises(KeyboardInterrupt, match="synthetic interrupt"):
        run_smoke(dispatch_timestamp=_DISPATCH)


def test_cli_sanitizes_malformed_timestamp(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _FailingSource()
    monkeypatch.setattr(smoke, "build_alpaca_iex_source", lambda: source)
    sentinel = "SECRET_KEY_VALUE_DO_NOT_LEAK"

    exit_code = smoke.main(["--dispatch-timestamp", sentinel])

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert exit_code == 1
    assert output == {
        "status": "FAILURE",
        "error": {
            "type": "PriceEvidenceError",
            "message": "market data evidence is invalid",
            "reason": "DISPATCH_TIMESTAMP_INVALID",
        },
    }
    assert sentinel not in captured.out + captured.err
    assert source.calls == 0


def test_runner_source_has_no_forbidden_authority_or_clock_imports() -> None:
    source = Path(smoke.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules: set[str] = set()
    imported_names: set[str] = set()
    referenced_names = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }
    forbidden_calls: list[ast.Call] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                imported_modules.add(node.module)
            imported_names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Call):
            function = node.func
            if isinstance(function, ast.Name) and function.id == "__import__":
                forbidden_calls.append(node)
            elif isinstance(function, ast.Attribute):
                if function.attr == "import_module":
                    forbidden_calls.append(node)
                if function.attr in {"acquire", "reveal"}:
                    forbidden_calls.append(node)

    forbidden_credential_imports = {
        module
        for module in imported_modules
        if module == "quantforge.credentials"
        or module.startswith("quantforge.credentials.")
    }
    forbidden_dynamic_import_modules = {
        module
        for module in imported_modules
        if module == "importlib" or module.startswith("importlib.")
    }
    assert not forbidden_credential_imports
    assert not forbidden_dynamic_import_modules
    assert "__import__" not in imported_names
    assert not (
        "credentials" in imported_names
        and "quantforge" in imported_modules
    )
    assert not forbidden_calls
    assert not (
        {
            "CredentialError",
            "CredentialBoundary",
            "CredentialLease",
            "CredentialPurpose",
            "SecretMaterial",
            "build_credential_boundary",
        }
        & (referenced_names | imported_names)
    )

    for forbidden in (
        "http.client",
        "ssl",
        "socket",
        "requests",
        "httpx",
        "aiohttp",
        "urllib",
        "websocket",
        "websockets",
        "CredWriteW",
        "datetime.now",
        "datetime.utcnow",
        "time.time",
    ):
        assert forbidden not in source


class _CoverageSource(_SyntheticSource):
    def _capture_smoke_observation(
        self,
        request: PriceObservationRequest,
        *,
        dispatch_timestamp: datetime,
    ) -> object:
        self.calls += 1
        self.capture_calls += 1
        self.requests.append(request)
        raise PriceCoverageError("COVERAGE_SECRET_SENTINEL")


class _ArgumentFailingSource(_SyntheticSource):
    def _capture_smoke_observation(
        self,
        request: PriceObservationRequest,
        *,
        dispatch_timestamp: datetime,
    ) -> object:
        self.calls += 1
        self.capture_calls += 1
        self.requests.append(request)
        raise PriceArgumentError("ARGUMENT_SECRET_SENTINEL")


class _MalformedObservationSource:
    source_id = _SOURCE_ID

    def __init__(self) -> None:
        self.calls = 0

    def _capture_smoke_observation(
        self,
        request: PriceObservationRequest,
        *,
        dispatch_timestamp: datetime,
    ) -> object:
        self.calls += 1
        return {"secret": "OBSERVATION_SECRET"}


def _failure_reason(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source: object,
    timestamp: str = "2030-01-01T12:02:00.000000Z",
) -> tuple[int, dict[str, object], str]:
    monkeypatch.setattr(smoke, "build_alpaca_iex_source", lambda: source)
    exit_code = smoke.main(["--dispatch-timestamp", timestamp])
    captured = capsys.readouterr()
    return exit_code, json.loads(captured.out), captured.out + captured.err


def test_reason_codes_are_closed_and_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sentinel = "SECRET_REASON_SENTINEL"
    cases = [
        (_FailingSource(), "SOURCE_FAILURE"),
        (_ArgumentFailingSource(_observation()), "SOURCE_FAILURE"),
        (_CoverageSource(_observation()), "SYMBOL_COVERAGE_INVALID"),
        (_MalformedObservationSource(), "OBSERVATION_INVALID"),
    ]
    for source, reason in cases:
        exit_code, result, output = _failure_reason(monkeypatch, capsys, source)
        assert exit_code == 1
        assert result["error"] == {
            "type": "PriceEvidenceError",
            "message": "market data evidence is invalid",
            "reason": reason,
        }
        assert sentinel not in output
        assert "failure sentinel" not in output
        assert "ARGUMENT_SECRET_SENTINEL" not in output
        assert "OBSERVATION_SECRET" not in output


def test_source_failure_does_not_retry_or_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _FailingSource()
    monkeypatch.setattr(smoke, "build_alpaca_iex_source", lambda: source)
    called = False

    def unexpected_replay(**kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("fallback observation must not be attempted")

    monkeypatch.setattr(smoke, "produce_price_freshness_evidence", unexpected_replay)
    with pytest.raises(PriceSourceError):
        run_smoke(dispatch_timestamp=_DISPATCH)

    assert source.calls == 1
    assert called is False


def test_post_observation_correspondence_failure_has_bounded_reason(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _SyntheticSource(_observation())
    monkeypatch.setattr(smoke, "build_alpaca_iex_source", lambda: source)
    monkeypatch.setattr(
        smoke,
        "_build_provenance",
        lambda *, dispatch_timestamp, observation: _provenance(
            prices={"AAPL": Decimal(1), "MSFT": Decimal(2), "SPY": Decimal(3)}
        ),
    )

    with pytest.raises(PriceProvenanceMismatchError) as failure:
        run_smoke(dispatch_timestamp=_DISPATCH)

    result = smoke._safe_failure(failure.value)
    assert result["error"]["reason"] == "PRICE_CORRESPONDENCE_MISMATCH"  # type: ignore[index]
    assert source.calls == 1


def test_replay_timestamp_correspondence_failure_has_bounded_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _SyntheticSource(_observation())
    monkeypatch.setattr(smoke, "build_alpaca_iex_source", lambda: source)

    def fail_replay(**kwargs: object) -> object:
        raise PriceTimestampError("TIMESTAMP_SECRET_SENTINEL")

    monkeypatch.setattr(smoke, "produce_price_freshness_evidence", fail_replay)

    with pytest.raises(PriceTimestampError) as failure:
        run_smoke(dispatch_timestamp=_DISPATCH)

    result = smoke._safe_failure(failure.value)
    assert result["error"]["reason"] == "PRICE_CORRESPONDENCE_MISMATCH"  # type: ignore[index]
    assert source.calls == 1


def test_cli_stale_failure_emits_only_the_bounded_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = _SyntheticSource(
        _observation(
            observed_at=_DISPATCH - timedelta(seconds=2),
            maximum_age=timedelta(seconds=1),
        )
    )
    monkeypatch.setattr(smoke, "build_alpaca_iex_source", lambda: source)

    exit_code = smoke.main(
        ["--dispatch-timestamp", "2030-01-01T12:02:00.000000Z"]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert output == {
        "status": "FAILURE",
        "error": {
            "type": "PriceEvidenceError",
            "message": "market data evidence is invalid",
            "reason": "OBSERVATION_STALE",
            "diagnostic": {
                "aggregate_age_category": "FRESH",
                "per_symbol_relation": {
                    "AAPL": "FRESH",
                    "MSFT": "FRESH",
                    "SPY": "FRESH",
                },
            },
        },
    }
    assert source.capture_calls == 1
    assert source.observe_calls == 0


def test_future_and_stale_observation_reason_codes_remain_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    future_source = _SyntheticSource(
        _observation(observed_at=_DISPATCH + timedelta(seconds=1))
    )
    monkeypatch.setattr(smoke, "build_alpaca_iex_source", lambda: future_source)
    with pytest.raises(PriceFutureObservationError) as future:
        run_smoke(dispatch_timestamp=_DISPATCH)
    assert smoke._safe_failure(future.value)["error"]["reason"] == "OBSERVATION_IN_FUTURE"  # type: ignore[index]

    stale_source = _SyntheticSource(
        _observation(
            observed_at=_DISPATCH - timedelta(seconds=2),
            maximum_age=timedelta(seconds=1),
        )
    )
    monkeypatch.setattr(smoke, "build_alpaca_iex_source", lambda: stale_source)
    with pytest.raises(PriceStaleObservationError) as stale:
        run_smoke(dispatch_timestamp=_DISPATCH)
    assert smoke._safe_failure(stale.value)["error"]["reason"] == "OBSERVATION_STALE"  # type: ignore[index]


def test_unknown_price_evidence_error_uses_fallback_reason() -> None:
    secret = "SECRET_CAUSE_SENTINEL"
    error = PriceEvidenceError(secret)
    error.__cause__ = ValueError(secret)
    result = smoke._safe_failure(error)
    assert result == {
        "status": "FAILURE",
        "error": {
            "type": "PriceEvidenceError",
            "message": "market data evidence is invalid",
            "reason": "PRICE_EVIDENCE_INVALID",
        },
    }
    assert secret not in json.dumps(result)


def test_invalid_dispatch_timestamp_reason_does_not_echo_caller_value(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sentinel = "SECRET_TIMESTAMP_SENTINEL"
    source = _FailingSource()
    exit_code, result, output = _failure_reason(monkeypatch, capsys, source, sentinel)
    assert exit_code == 1
    assert result["error"]["reason"] == "DISPATCH_TIMESTAMP_INVALID"  # type: ignore[index]
    assert sentinel not in output
    assert source.calls == 0



def test_reason_code_vocabulary_is_exactly_closed() -> None:
    assert smoke._REASON_CODES == (
        "DISPATCH_TIMESTAMP_INVALID",
        "SOURCE_FAILURE",
        "SYMBOL_COVERAGE_INVALID",
        "OBSERVATION_INVALID",
        "PRICE_CORRESPONDENCE_MISMATCH",
        "OBSERVATION_IN_FUTURE",
        "OBSERVATION_STALE",
        "PRICE_EVIDENCE_INVALID",
    )


def test_cause_and_context_are_never_rendered() -> None:
    secret = "SECRET_CAUSE_CONTEXT_SENTINEL"
    try:
        raise ValueError(secret)
    except ValueError as context:
        error = PriceEvidenceError(secret)
        error.__context__ = context
        error.__cause__ = context

    result = smoke._safe_failure(error)
    rendered = json.dumps(result, sort_keys=True)
    assert secret not in rendered
    assert result["error"]["reason"] == "PRICE_EVIDENCE_INVALID"  # type: ignore[index]
