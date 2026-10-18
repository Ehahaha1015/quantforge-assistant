from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

from quantforge.domain import Bar, Experiment, Fill, OrderIntent, Position, Signal

STAMP = datetime(2025, 1, 1, 12, tzinfo=UTC)


def test_valid_models_and_json_round_trip() -> None:
    models = [
        Bar(symbol="AAPL", timestamp=STAMP, open="10", high="12", low="9", close="11", volume="100"),
        Signal(strategy_id="s1", symbol="AAPL", timestamp=STAMP, value="0.5"),
        OrderIntent(strategy_id="s1", symbol="AAPL", timestamp=STAMP, side="BUY", quantity="2"),
        Fill(order_id="o1", symbol="AAPL", timestamp=STAMP, side="SELL", quantity="2", price="11", fee="0.1"),
        Position(symbol="AAPL", quantity="-2", average_price="11"),
        Experiment(
            experiment_id="e1",
            git_commit="abc",
            dataset_hash="def",
            random_seed=7,
            started_at=STAMP,
        ),
    ]
    for model in models:
        assert type(model).model_validate_json(model.model_dump_json()) == model


def test_equivalent_decimal_values_serialize_consistently() -> None:
    first = Signal(strategy_id="s", symbol="AAPL", timestamp=STAMP, value="0.50")
    second = Signal(strategy_id="s", symbol="AAPL", timestamp=STAMP, value="0.5")
    assert first == second
    assert first.model_dump_json() == second.model_dump_json()


def test_decimal_canonicalization_preserves_arbitrary_precision() -> None:
    given = Decimal(99999999999999999999999999999)
    bar = Bar(symbol="AAPL", timestamp=STAMP, open=given, high=given, low=given, close=given, volume=0)
    assert bar.open == given


def test_decimal_canonicalization_is_independent_of_ambient_context() -> None:
    with localcontext() as context:
        context.prec = 2
        signal = Signal(strategy_id="s", symbol="AAPL", timestamp=STAMP, value=Decimal("0.123456"))
        position = Position(symbol="AAPL", quantity=Decimal("123.456"), average_price=10)
    assert signal.value == Decimal("0.123456")
    assert position.quantity == Decimal("123.456")


@pytest.mark.parametrize("value", ["0", "-0", "-0.00"])
def test_signed_zero_has_canonical_json(value: str) -> None:
    signal = Signal(strategy_id="s", symbol="AAPL", timestamp=STAMP, value=value)
    position = Position(symbol="AAPL", quantity=value, average_price=10)
    assert signal.model_dump_json() == Signal(strategy_id="s", symbol="AAPL", timestamp=STAMP, value=0).model_dump_json()
    assert position.model_dump_json() == Position(symbol="AAPL", quantity=0, average_price=10).model_dump_json()


def test_datetime_utc_overflow_is_validation_error() -> None:
    value = datetime(1, 1, 1, 3, tzinfo=timezone(timedelta(hours=5)))
    with pytest.raises(ValidationError):
        Signal(strategy_id="s", symbol="AAPL", timestamp=value, value=0)


@pytest.mark.parametrize("model_type", [Bar, Signal, OrderIntent, Fill, Experiment])
def test_naive_datetime_rejected(model_type: type) -> None:
    fields = {
        Bar: {"symbol": "AAPL", "timestamp": datetime.fromisoformat("2025-01-01T00:00:00"), "open": 1, "high": 1, "low": 1, "close": 1, "volume": 0},
        Signal: {"strategy_id": "s", "symbol": "AAPL", "timestamp": datetime.fromisoformat("2025-01-01T00:00:00"), "value": 0},
        OrderIntent: {"strategy_id": "s", "symbol": "AAPL", "timestamp": datetime.fromisoformat("2025-01-01T00:00:00"), "side": "BUY", "quantity": 1},
        Fill: {"order_id": "o", "symbol": "AAPL", "timestamp": datetime.fromisoformat("2025-01-01T00:00:00"), "side": "BUY", "quantity": 1, "price": 1, "fee": 0},
        Experiment: {"experiment_id": "e", "git_commit": "g", "dataset_hash": "d", "random_seed": 1, "started_at": datetime.fromisoformat("2025-01-01T00:00:00")},
    }
    with pytest.raises(ValidationError):
        model_type(**fields[model_type])


def test_non_utc_datetime_is_canonicalized() -> None:
    value = STAMP.astimezone(timezone(timedelta(hours=5, minutes=30)))
    signal = Signal(strategy_id="s", symbol="AAPL", timestamp=value, value=0)
    assert signal.timestamp == STAMP
    assert signal.timestamp.tzinfo == UTC


def test_unexpected_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        Signal(strategy_id="s", symbol="AAPL", timestamp=STAMP, value=0, extra="no")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"open": 0},
        {"open": -1},
        {"volume": -1},
        {"low": 13},
        {"open": 8},
        {"high": 9},
        {"close": 8},
        {"close": 13},
    ],
)
def test_invalid_bar_rejected(kwargs: dict[str, object]) -> None:
    base = {"symbol": "AAPL", "timestamp": STAMP, "open": 10, "high": 12, "low": 9, "close": 11, "volume": 100}
    base.update(kwargs)
    with pytest.raises(ValidationError):
        Bar(**base)


@pytest.mark.parametrize("value", [-1, 1])
def test_signal_boundaries(value: int) -> None:
    assert Signal(strategy_id="s", symbol="AAPL", timestamp=STAMP, value=value).value == value


@pytest.mark.parametrize("value", [-1.1, 1.1, "NaN", "Infinity"])
def test_signal_invalid_values(value: object) -> None:
    with pytest.raises(ValidationError):
        Signal(strategy_id="s", symbol="AAPL", timestamp=STAMP, value=value)


def test_order_intent_validation() -> None:
    for side in ("BUY", "SELL"):
        assert OrderIntent(strategy_id="s", symbol="AAPL", timestamp=STAMP, side=side, quantity=1).side == side
    for side, quantity in [("HOLD", 1), ("BUY", 0), ("BUY", -1)]:
        with pytest.raises(ValidationError):
            OrderIntent(strategy_id="s", symbol="AAPL", timestamp=STAMP, side=side, quantity=quantity)


@pytest.mark.parametrize("kwargs", [{"quantity": 0}, {"quantity": -1}, {"price": 0}, {"price": -1}, {"fee": -1}])
def test_fill_validation(kwargs: dict[str, object]) -> None:
    base = {"order_id": "o", "symbol": "AAPL", "timestamp": STAMP, "side": "BUY", "quantity": 1, "price": 1, "fee": 0}
    base.update(kwargs)
    with pytest.raises(ValidationError):
        Fill(**base)


def test_position_allows_zero_and_negative_quantity() -> None:
    assert Position(symbol="AAPL", quantity=0, average_price=10).quantity == 0
    assert Position(symbol="AAPL", quantity=-1, average_price=10).quantity == -1
