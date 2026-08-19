"""Tests for the percentile utility shared with Alpaca SQLite qualification."""

from __future__ import annotations

import math

import pytest

from app.services.account_custody_qualification import nist_r6_percentile


def test_nist_r6_percentiles_match_reference_examples() -> None:
    wafer_measurements_scaled = tuple(
        round(value * 10_000)
        for value in (
            95.1772,
            95.1567,
            95.1937,
            95.1959,
            95.1442,
            95.061,
            95.1591,
            95.1195,
            95.1065,
            95.0925,
            95.199,
            95.1682,
            95.1162,
            95.1137,
            95.0717,
            95.219,
            95.1491,
            95.1163,
            95.105,
            95.1403,
        )
    )

    assert math.isclose(nist_r6_percentile((1, 2, 3, 4), 50), 2.5)
    assert math.isclose(
        nist_r6_percentile(wafer_measurements_scaled, 90),
        951_986.9,
        abs_tol=0.001,
    )
    assert nist_r6_percentile((1, 2, 3, 4), 95) == 4
    assert nist_r6_percentile((1, 2, 3, 4), 99) == 4


@pytest.mark.parametrize(
    ("values", "percentile"),
    [((), 50), ((1,), 0), ((1,), 101), ((-1,), 50)],
)
def test_nist_r6_percentile_rejects_invalid_inputs(
    values: tuple[int, ...],
    percentile: int,
) -> None:
    with pytest.raises(ValueError):
        nist_r6_percentile(values, percentile)
