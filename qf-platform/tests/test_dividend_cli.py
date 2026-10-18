from __future__ import annotations

from qf_platform.dividend_cli import _index_events


def test_structured_dividend_index_parses_exact_share_units() -> None:
    source = (
        "<table><tr><td>2023</td><td>2023-01-13</td><td>2023-01-16</td>"
        "<td>每10份派现金0.6400元</td><td>2023-01-19</td></tr></table>"
    ).encode()
    assert _index_events(source) == [
        {
            "record": "2023-01-13",
            "ex": "2023-01-16",
            "pay": "2023-01-19",
            "amount": "0.064",
        }
    ]
