"""The ticker shape and the symbol-list setting rule (#3525)."""

from __future__ import annotations

import pytest

from app.schemas.broker_bots import normalized_symbol as wire_normalized_symbol
from app.utils.symbols import normalized_symbol, parse_symbol_list


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("SPY,QQQ,IWM", ("SPY", "QQQ", "IWM")), (" spy , QQQ,spy,", ("SPY", "QQQ")), ("", ()), (" , ", ()), ("BRK.B,rds-a", ("BRK.B", "RDS-A"))],
)
def test_a_symbol_list_is_split_upper_cased_and_deduplicated(raw, expected):
    assert parse_symbol_list(raw, setting="X") == expected


@pytest.mark.parametrize("raw", ["SPY,9X", "../SPY", "SPY QQQ", "A" * 13])
def test_a_token_that_is_not_a_ticker_names_the_setting_and_the_token(raw):
    token = raw.split(",")[-1].strip()

    with pytest.raises(ValueError, match=rf"MY_SETTING names {token!r}"):
        parse_symbol_list(raw, setting="MY_SETTING")


def test_the_wire_models_and_the_setting_apply_one_rule():
    """``app.schemas.broker_bots`` re-uses this module's rule rather than holding a second copy."""
    assert wire_normalized_symbol is normalized_symbol
