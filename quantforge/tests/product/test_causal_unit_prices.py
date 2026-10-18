from datetime import UTC, datetime
from decimal import Decimal, localcontext

import polars as pl
import pytest

from quantforge.product.causal_unit_prices import causal_close_window, unit_momentum
from tests.product.test_share_adjustments import event, frame


def test_midnight_label_cannot_expose_same_day_close_before_16_shanghai() -> None:
    observed = causal_close_window(
        frame(),
        as_of=datetime(2020, 1, 5, 2, tzinfo=UTC),
        lookback=2,
        adjustments=(event(),),
    )
    assert observed["unit_reference_session"] == "2020-01-04"
    assert observed["closes"] == ["10.0", "10.0"]
    assert all(not row["event_refs"] for row in observed["provenance"])


def test_split_units_remove_mechanical_loss_only_after_current_unit_quote_visible() -> (
    None
):
    result = unit_momentum(
        frame(),
        as_of=datetime(2020, 1, 5, 8, tzinfo=UTC),
        lookback=2,
        threshold_pct=Decimal(0),
        adjustments=(event(),),
    )
    assert Decimal(result["change_pct"]) == 0
    assert result["window"]["closes"] == ["5.0", "5.0", "5.0"]
    assert result["window"]["provenance"][-1]["event_refs"] == []
    with localcontext() as context:
        context.prec = 5
        assert (
            unit_momentum(
                frame(),
                as_of=datetime(2020, 1, 5, 8, tzinfo=UTC),
                lookback=2,
                threshold_pct=Decimal(0),
                adjustments=(event(),),
            )
            == result
        )


def test_future_shock_cannot_change_visible_window_or_its_hash() -> None:
    source = frame()
    changed = (
        source.with_row_index("i")
        .with_columns(
            *(
                pl.when(pl.col("i") >= 6)
                .then(pl.col(k) * 9)
                .otherwise(pl.col(k))
                .alias(k)
                for k in ("open", "high", "low", "close")
            )
        )
        .drop("i")
    )
    args = {
        "as_of": datetime(2020, 1, 5, 8, tzinfo=UTC),
        "lookback": 3,
        "adjustments": (event(),),
    }
    assert causal_close_window(source, **args) == causal_close_window(changed, **args)


def test_visible_unverified_transition_and_insufficient_data_are_rejected() -> None:
    with pytest.raises(ValueError, match="unverified"):
        causal_close_window(
            frame(),
            as_of=datetime(2020, 1, 5, 8, tzinfo=UTC),
            lookback=3,
            adjustments=(event(verified=False),),
        )
    with pytest.raises(ValueError, match="insufficient"):
        causal_close_window(
            frame(),
            as_of=datetime(2020, 1, 1, 2, tzinfo=UTC),
            lookback=1,
            adjustments=(),
        )


@pytest.mark.parametrize("lookback", [0, True, 1000])
def test_invalid_lookback_cannot_become_zero_horizon_or_bool_intent(
    lookback: int,
) -> None:
    with pytest.raises(ValueError, match="integer momentum"):
        unit_momentum(
            frame(),
            as_of=datetime(2020, 1, 5, 8, tzinfo=UTC),
            lookback=lookback,
            threshold_pct=Decimal(0),
            adjustments=(event(),),
        )
