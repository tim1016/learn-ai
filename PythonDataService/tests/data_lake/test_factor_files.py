"""Parity tests for the factor-file read side (app/data_lake/factor_files.py).

The oracle is a direct Python port of LEAN's
``CorporateFactorProvider.GetScalingFactors`` (QuantConnect/Lean,
``Common/Data/Auxiliary/CorporateFactorProvider.cs``, master): iterate the
factor-file dates newest-to-oldest, keep overwriting while the row date is
≥ the search date, break at the first row older than it; a search date
newer than every row resolves to the identity. The canonical
``factor_multiplier_as_of`` must agree with that walk for every date,
including on-row, between-row, before-first, and after-last boundaries.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.data_lake.factor_files import FactorRow, factor_multiplier_as_of, parse_factor_file


def _lean_get_scaling_factors(rows: list[FactorRow], search_date: date) -> tuple[Decimal, Decimal]:
    """Verbatim port of LEAN's GetScalingFactors walk (newest → oldest)."""
    reversed_rows = sorted(rows, key=lambda r: r.row_date, reverse=True)
    price_factor, split_factor = Decimal(1), Decimal(1)  # the identity default
    for row in reversed_rows:
        if row.row_date < search_date:
            break
        price_factor, split_factor = row.price_factor, row.split_factor
    return price_factor, split_factor


def _rows(*specs: tuple[str, str, str]) -> list[FactorRow]:
    return [
        FactorRow(date.fromisoformat(d), Decimal(pf), Decimal(sf))
        for d, pf, sf in specs
    ]


def test_factor_multiplier_matches_lean_getscaling_factors_over_a_date_grid() -> None:
    rows = _rows(
        ("2024-01-02", "0.95", "0.5"),   # oldest cumulative factors
        ("2024-06-28", "0.97", "0.5"),   # a dividend ex-date on 2024-07-01
        ("2024-11-29", "0.99", "1"),     # a split ex-date on 2024-12-02
        ("2026-04-30", "1", "1"),        # the terminal anchor row
    )
    # Walk a contiguous 900-day grid across and beyond the rows: before the
    # first row, on rows, between rows, and past the last row.
    day = date(2023, 12, 1)
    for _ in range(900):
        expected = _lean_get_scaling_factors(rows, day)
        assert factor_multiplier_as_of(rows, day) == expected[0] * expected[1], day
        day += timedelta(days=1)


def test_parse_factor_file_non_numeric_factor_rejected() -> None:
    # decimal.InvalidOperation is an ArithmeticError, not a ValueError — the
    # handler must still deliver the documented malformed-row ValueError.
    with pytest.raises(ValueError, match="malformed factor file row"):
        parse_factor_file("20240701,abc,1,99\n")


def test_factor_multiplier_empty_file_is_identity() -> None:
    assert factor_multiplier_as_of([], date(2024, 7, 1)) == Decimal(1)


def test_factor_multiplier_after_last_row_is_identity() -> None:
    rows = _rows(("2024-07-01", "0.99", "0.5"))
    # LEAN's loop breaks on the first (only) row when it is older than the
    # search date, leaving the initialized identity — post-file data is
    # unadjusted.
    assert factor_multiplier_as_of(rows, date(2024, 7, 2)) == Decimal(1)
    assert factor_multiplier_as_of(rows, date(2024, 7, 1)) == Decimal("0.99") * Decimal("0.5")


def test_factor_multiplier_builder_round_trip() -> None:
    """The builder's own file layout resolves with event-day continuity.

    build_factor_file_bytes dates an event's row at the session before the
    ex-date; reading it back with the LEAN lookup must apply the event's
    factors to data ≤ that row and not after — the round trip the study's
    adjustment depends on.
    """
    from app.data_lake.factor_files import build_factor_file_bytes, plan_factor_file
    from app.data_lake.polygon_corp_actions import SplitEvent

    # 2:1 split with ex-date 2024-07-05; raw closes 200 before, 100 after.
    closes = {date(2024, 7, 1): Decimal(200), date(2024, 7, 2): Decimal(201), date(2024, 7, 3): Decimal(202), date(2024, 7, 5): Decimal(100)}
    plan = plan_factor_file(
        closes, [SplitEvent(execution_date="2024-07-05", split_from=1, split_to=2)], []
    )
    body = build_factor_file_bytes("SPY", plan, closes)
    rows = parse_factor_file(body.decode("ascii"))

    # Pre-split data carries the 0.5 split factor; the ex-date and after do
    # not — adjusted closes are continuous across the split.
    pre = factor_multiplier_as_of(rows, date(2024, 7, 3))
    post = factor_multiplier_as_of(rows, date(2024, 7, 5))
    assert pre == Decimal("0.5")
    assert post == Decimal(1)
    assert float(Decimal(202) * pre) == pytest.approx(101.0)
    assert float(Decimal(100) * post) == 100.0
