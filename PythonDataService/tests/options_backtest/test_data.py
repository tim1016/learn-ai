"""The pin a run keeps of what it read."""

from __future__ import annotations

import hashlib

import pytest

from app.options_backtest.data import Captured


def test_a_value_is_captured_with_the_sha256_of_its_file() -> None:
    pin = hashlib.sha256(b"one stored file").hexdigest()

    assert Captured(value=("bar",), sha256=pin).sha256 == pin


@pytest.mark.parametrize(
    "not_a_pin",
    [
        "",
        "a" * 63,
        "a" * 65,
        "A" * 64,  # a hash is written in lower case, one way
        "g" * 64,
        " " + "a" * 63,
    ],
)
def test_a_pin_that_is_not_a_sha256_pins_nothing_and_is_refused(not_a_pin: str) -> None:
    with pytest.raises(ValueError, match="sha256"):
        Captured(value=(), sha256=not_a_pin)
