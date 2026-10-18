"""Synthetic source, parser, identity and mutation coverage."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, is_dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from _marketdata_alpaca_fixtures import (
    SUBSETS,
    SYMBOLS,
    RecordingFetch,
    make_body,
    make_request,
    make_response,
    make_trade,
)

import quantforge.marketdata_alpaca.source as source_module
from quantforge.marketdata import (
    PriceCoverageError,
    PriceFactError,
    PriceSourceError,
    PriceTimestampError,
)
from quantforge.marketdata.canonical import _observation_id as price_observation_id
from quantforge.marketdata_alpaca.source import _create_source


def _observe(
    symbols: tuple[str, ...] = SYMBOLS,
    *,
    body: bytes | None = None,
) -> tuple[object, RecordingFetch]:
    resolved_body = make_body(symbols) if body is None else body
    fetch = RecordingFetch(
        response=make_response(
            resolved_body,
            content_length=str(len(resolved_body)),
        )
    )
    source = _create_source(fetch=fetch)
    return source, fetch


def test_all_frozen_symbol_subsets_make_one_canonical_fetch() -> None:
    for symbols in SUBSETS:
        source, fetch = _observe(symbols)
        observation = source.observe(make_request(symbols))
        assert len(fetch.calls) == 1
        assert fetch.calls[0] == (
            "/v2/stocks/trades/latest?symbols="
            + ",".join(symbols)
            + "&feed=iex"
        )
        assert tuple(fact.symbol for fact in observation.risk_prices) == symbols


def test_noncanonical_or_out_of_scope_request_fails_before_fetch() -> None:
    fetch = RecordingFetch(
        response=make_response(make_body(("AAPL",)), content_length="1")
    )
    source = _create_source(fetch=fetch)
    request = type(make_request(("AAPL",))).model_construct(
        schema="marketdata-request-v1", symbols=("MSFT", "AAPL")
    )
    with pytest.raises(PriceCoverageError) as info:
        source.observe(request)
    assert str(info.value) == (
        "observation symbol coverage does not match the required symbols"
    )
    assert info.value.__cause__ is None
    assert info.value.__context__ is None
    assert fetch.calls == []

    request = type(make_request(("AAPL",))).model_construct(
        schema="marketdata-request-v1", symbols=("AAPL", "QQQ")
    )
    with pytest.raises(PriceCoverageError):
        source.observe(request)
    assert fetch.calls == []


def test_response_coverage_and_structure_fail_closed() -> None:
    coverage_cases = (
        {"MSFT": make_trade("MSFT")},
        {
            "AAPL": make_trade("AAPL"),
            "MSFT": make_trade("MSFT"),
            "SPY": make_trade("SPY"),
            "QQQ": make_trade("AAPL"),
        },
        {},
    )
    for trades in coverage_cases:
        source, _ = _observe(
            body=json.dumps({"trades": trades}).encode("utf-8")
        )
        with pytest.raises(PriceCoverageError) as info:
            source.observe(make_request(SYMBOLS))
        assert str(info.value) == (
            "observation symbol coverage does not match the required symbols"
        )
        assert info.value.__cause__ is None
        assert info.value.__context__ is None

    malformed_cases = (
        [],
        None,
        {
            "AAPL": make_trade("AAPL", update="canceled"),
            "MSFT": make_trade("MSFT"),
            "SPY": make_trade("SPY"),
        },
    )
    for trades in malformed_cases:
        source, _ = _observe(
            body=json.dumps({"trades": trades}).encode("utf-8")
        )
        with pytest.raises(PriceSourceError) as info:
            source.observe(make_request(SYMBOLS))
        assert str(info.value) == "price observation response was malformed"
        assert info.value.__cause__ is None
        assert info.value.__context__ is None


def test_duplicate_unknown_and_nonfinite_json_fail_closed() -> None:
    bodies = (
        (
            b'{"trades":{"AAPL":{"t":"2026-09-28T14:03:21.123456789Z",'
            b'"i":1,"x":"V","p":178.26,"s":1,"c":[],"z":"C",'
            b'"z":"C"}}}'
        ),
        (
            b'{"trades":{"AAPL":{"t":"2026-09-28T14:03:21.123456789Z",'
            b'"i":1,"x":"V","p":178.26,"s":1,"c":[],"z":"C",'
            b'"q":1}}}'
        ),
        (
            b'{"trades":{"AAPL":{"t":"2026-09-28T14:03:21.123456789Z",'
            b'"i":1,"x":"V","p":NaN,"s":1,"c":[],"z":"C"}}}'
        ),
        (
            b'{"trades":{"AAPL":{"t":"2026-09-28T14:03:21.123456789Z",'
            b'"i":1,"x":"V","p":178.26,"s":1,"c":[],"z":"C"}},'
            b'"currency":"EUR"}'
        ),
    )
    for body in bodies:
        source, _ = _observe(symbols=("AAPL",), body=body)
        with pytest.raises(PriceSourceError) as info:
            source.observe(make_request(("AAPL",)))
        assert str(info.value) == "price observation response was malformed"
        assert info.value.__cause__ is None
        assert info.value.__context__ is None


def test_currency_and_corrected_update_rules() -> None:
    for currency in (None, "USD"):
        body = make_body(("AAPL",), currency=currency)
        source, _ = _observe(symbols=("AAPL",), body=body)
        assert source.observe(make_request(("AAPL",))).risk_prices[0].price == Decimal("178.26")

    for update in ("corrected", None):
        body = make_body(
            ("AAPL",), trades={"AAPL": make_trade("AAPL", update=update)}
        )
        source, _ = _observe(symbols=("AAPL",), body=body)
        assert source.observe(make_request(("AAPL",))).source_observation_id

    for currency in ("EUR", 1, [], False):
        payload = {"trades": {"AAPL": make_trade("AAPL")}, "currency": currency}
        source, _ = _observe(symbols=("AAPL",), body=json.dumps(payload).encode())
        with pytest.raises(PriceSourceError, match="response was malformed"):
            source.observe(make_request(("AAPL",)))

    for update in ("canceled", "incorrect", "other", 1, [], None):
        trade = make_trade("AAPL", update=update)
        if update is None:
            trade["u"] = None
        payload = {"trades": {"AAPL": trade}}
        source, _ = _observe(symbols=("AAPL",), body=json.dumps(payload).encode())
        with pytest.raises(PriceSourceError, match="response was malformed"):
            source.observe(make_request(("AAPL",)))


def test_price_bounds_and_decimal_rules() -> None:
    accepted = (1, Decimal("0.000001"), Decimal("9999999.999999"))
    for value in accepted:
        trade = make_trade("AAPL", price=value)
        body = json.dumps(
            {"trades": {"AAPL": trade}}, default=str
        ).encode()
        if isinstance(value, Decimal):
            body = body.replace(
                json.dumps(str(value)).encode(),
                format(value, "f").encode(),
            )
        source, _ = _observe(symbols=("AAPL",), body=body)
        observation = source.observe(make_request(("AAPL",)))
        assert observation.risk_prices[0].price == Decimal(str(value))

    rejected = (0, -1, 10000000, 1.1234567)
    for value in rejected:
        trade = make_trade("AAPL", price=value)
        body = json.dumps({"trades": {"AAPL": trade}}).encode()
        source, _ = _observe(symbols=("AAPL",), body=body)
        with pytest.raises(PriceFactError) as info:
            source.observe(make_request(("AAPL",)))
        assert str(info.value) == "risk price must be a finite positive Decimal"
        assert info.value.__cause__ is None
        assert info.value.__context__ is None

    malformed = ("178.26", True, None, [])
    for value in malformed:
        trade = make_trade("AAPL", price=value)
        if value is None:
            trade["p"] = None
        body = json.dumps({"trades": {"AAPL": trade}}).encode()
        source, _ = _observe(symbols=("AAPL",), body=body)
        with pytest.raises(PriceSourceError) as info:
            source.observe(make_request(("AAPL",)))
        assert str(info.value) == "price observation response was malformed"
        assert info.value.__cause__ is None
        assert info.value.__context__ is None


def test_timestamp_grammar_truncates_toward_earlier_microsecond() -> None:
    timestamp = "2026-09-28T14:03:21.123456789Z"
    source, _ = _observe(
        symbols=("AAPL",),
        body=make_body(
            ("AAPL",),
            trades={"AAPL": make_trade("AAPL", timestamp=timestamp)},
        ),
    )
    observation = source.observe(make_request(("AAPL",)))
    assert observation.observed_at == datetime(
        2026, 9, 28, 14, 3, 21, 123456, tzinfo=UTC
    )

    for timestamp in (
        "2026-09-28T14:03:21Z",
        "2026-09-28T14:03:21.1234567890Z",
        "2026-09-28T14:03:21.123456789+00:00",
        "2026-02-29T14:03:21.123456Z",
        "2026-09-28T24:03:21.123456Z",
        "2026-09-28T14:03:61.123456Z",
    ):
        source, _ = _observe(
            symbols=("AAPL",),
            body=make_body(
                ("AAPL",),
                trades={"AAPL": make_trade("AAPL", timestamp=timestamp)},
            ),
        )
        with pytest.raises(PriceTimestampError) as info:
            source.observe(make_request(("AAPL",)))
        assert str(info.value) == "observed_at is malformed"
        assert info.value.__cause__ is None
        assert info.value.__context__ is None


def test_trade_id_is_composite_identity_input_and_other_fields_are_not() -> None:
    base_source, _ = _observe(symbols=("AAPL",))
    base = base_source.observe(make_request(("AAPL",)))
    for field in ("x", "s", "c", "z"):
        trade = make_trade("AAPL")
        trade[field] = {"x": "Q", "s": 7, "c": ["@", "T"], "z": "A"}[field]
        source, _ = _observe(
            symbols=("AAPL",),
            body=make_body(("AAPL",), trades={"AAPL": trade}),
        )
        mutated = source.observe(make_request(("AAPL",)))
        assert mutated.observation_id == base.observation_id
        assert mutated.source_observation_id == base.source_observation_id

    for field, value in (
        ("i", 2),
        ("p", 179),
        ("t", "2026-09-28T14:03:21.123456788Z"),
    ):
        trade = make_trade("AAPL")
        trade[field] = value
        source, _ = _observe(
            symbols=("AAPL",),
            body=make_body(("AAPL",), trades={"AAPL": trade}),
        )
        mutated = source.observe(make_request(("AAPL",)))
        if field == "p":
            assert mutated.source_observation_id == base.source_observation_id
            assert mutated.observation_id != base.observation_id
        else:
            assert mutated.source_observation_id != base.source_observation_id
            assert mutated.observation_id != base.observation_id


def test_observation_identity_rederives_with_price_001_oracle() -> None:
    source, _ = _observe()
    observation = source.observe(make_request(SYMBOLS))
    expected = price_observation_id(
        market_source=observation.market_source,
        source_observation_id=observation.source_observation_id,
        observed_at=observation.observed_at,
        maximum_age=observation.maximum_age,
        risk_prices=observation.risk_prices,
    )
    assert observation.observation_id == expected
    assert observation.observation_id.startswith("marketdata-obs-v1-")
    assert observation.source_observation_id.startswith("alpaca-iex-trade-v1-")


def test_transport_and_parser_errors_are_sanitized() -> None:
    from quantforge.marketdata_alpaca import transport

    for error in (
        transport._TransportUnavailable(),
        transport._ResponseMalformed(),
        transport._SourceRejected(),
        transport._SourceUnavailable(),
        transport._SourceRateLimited(),
        transport._InvalidTransportRequest(),
    ):
        source = _create_source(fetch=RecordingFetch(error=error))
        with pytest.raises(Exception) as info:
            source.observe(make_request(("AAPL",)))
        assert info.value.__cause__ is None
        assert info.value.__context__ is None
        assert "AAPL" not in str(info.value)


def _timestamp_for_age(dispatch: datetime, age_microseconds: int) -> str:
    observed = dispatch - timedelta(microseconds=age_microseconds)
    return observed.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _capture_for_ages(
    ages: tuple[int, int, int],
    *,
    dispatch: datetime,
) -> object:
    trades = {
        symbol: make_trade(
            symbol,
            timestamp=_timestamp_for_age(dispatch, age),
        )
        for symbol, age in zip(SYMBOLS, ages, strict=True)
    }
    body = make_body(SYMBOLS, trades=trades)
    fetch = RecordingFetch(
        response=make_response(body, content_length=str(len(body)))
    )
    source = _create_source(fetch=fetch)
    return source._capture_smoke_observation(
        make_request(SYMBOLS), dispatch_timestamp=dispatch
    )


def test_capture_uses_one_shared_fetch_parse_and_same_tuple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, fetch = _observe()
    parsed_ids: list[int] = []
    observation_ids: list[int] = []
    diagnostic_ids: list[int] = []
    original_parse = source_module.client._parse_response
    original_observation = source_module._build_observation
    original_diagnostic = source_module._build_diagnostic

    def parse(*args: object, **kwargs: object) -> object:
        parsed = original_parse(*args, **kwargs)  # type: ignore[arg-type]
        parsed_ids.append(id(parsed))
        return parsed

    def observation(parsed: object) -> object:
        observation_ids.append(id(parsed))
        return original_observation(parsed)  # type: ignore[arg-type]

    def diagnostic(
        parsed: object, **kwargs: object
    ) -> object:
        diagnostic_ids.append(id(parsed))
        return original_diagnostic(parsed, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(source_module.client, "_parse_response", parse)
    monkeypatch.setattr(source_module, "_build_observation", observation)
    monkeypatch.setattr(source_module, "_build_diagnostic", diagnostic)

    captured = source._capture_smoke_observation(
        make_request(SYMBOLS),
        dispatch_timestamp=datetime(2026, 9, 28, 14, 3, 22, tzinfo=UTC),
    )

    assert len(fetch.calls) == 1
    assert parsed_ids == observation_ids == diagnostic_ids
    assert len(parsed_ids) == 1
    assert captured.observation.risk_prices[0].symbol == "AAPL"


def test_capture_diagnostic_has_exact_boundaries_and_symbol_order() -> None:
    dispatch = datetime(2026, 9, 28, 14, 3, 22, tzinfo=UTC)
    cases = (
        ((-1, 0, 15_000_000), ("FUTURE", "FRESH", "FRESH")),
        ((15_000_001, 15_000_002, 20_000_000),
         ("STALE_THRESHOLD", "STALE", "STALE")),
    )
    for ages, expected in cases:
        captured = _capture_for_ages(ages, dispatch=dispatch)
        assert captured.diagnostic.aggregate_age_category == (
            "FRESH" if ages == (-1, 0, 15_000_000) else "STALE"
        )
        assert captured.diagnostic.per_symbol_relation == tuple(
            zip(SYMBOLS, expected, strict=True)
        )


def test_capture_diagnostic_uses_aggregate_minimum_and_truncated_timestamp() -> None:
    dispatch = datetime(2026, 9, 28, 14, 3, 22, tzinfo=UTC)
    trades = {
        "AAPL": make_trade(
            "AAPL",
            timestamp="2026-09-28T14:03:06.999999999Z",
        ),
        "MSFT": make_trade(
            "MSFT",
            timestamp="2026-09-28T14:03:22.000000999Z",
        ),
        "SPY": make_trade(
            "SPY",
            timestamp="2026-09-28T14:03:21.999999999Z",
        ),
    }
    body = make_body(SYMBOLS, trades=trades)
    source = _create_source(
        fetch=RecordingFetch(
            response=make_response(body, content_length=str(len(body)))
        )
    )

    captured = source._capture_smoke_observation(
        make_request(SYMBOLS), dispatch_timestamp=dispatch
    )

    assert captured.observation.observed_at == datetime(
        2026, 9, 28, 14, 3, 6, 999999, tzinfo=UTC
    )
    assert captured.diagnostic.aggregate_age_category == "STALE_THRESHOLD"
    assert captured.diagnostic.per_symbol_relation == (
        ("AAPL", "STALE_THRESHOLD"),
        ("MSFT", "FRESH"),
        ("SPY", "FRESH"),
    )


def test_capture_snapshot_is_immutable_and_has_no_source_state() -> None:
    dispatch = datetime(2026, 9, 28, 14, 3, 22, tzinfo=UTC)
    captured = _capture_for_ages((0, 0, 0), dispatch=dispatch)
    assert is_dataclass(captured)
    assert is_dataclass(captured.diagnostic)
    with pytest.raises(FrozenInstanceError):
        captured.diagnostic.aggregate_age_category = "STALE"  # type: ignore[misc]
