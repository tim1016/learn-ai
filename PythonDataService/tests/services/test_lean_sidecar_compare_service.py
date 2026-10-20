"""Tests for lean_sidecar_compare_service.reconcile_trade_lists."""

from __future__ import annotations

from decimal import Decimal

import pytest


def _trade(
    n: int,
    entry_ms: int,
    exit_ms: int,
    entry_price: Decimal | float,
    exit_price: Decimal | float,
    qty: Decimal | float = Decimal("10"),
    fee: Decimal | float | None = None,
) -> dict:
    ep = Decimal(str(entry_price))
    xp = Decimal(str(exit_price))
    q = Decimal(str(qty))
    t: dict = {
        "trade_number": n,
        "entry_ms_utc": entry_ms,
        "exit_ms_utc": exit_ms,
        "entry_price": ep,
        "exit_price": xp,
        "quantity": q,
        "pnl": (xp - ep) * q,
        "signal_reason": "test",
        "is_synthetic_exit": False,
    }
    if fee is not None:
        t["fee"] = Decimal(str(fee))
    return t


@pytest.mark.parametrize(
    "trades",
    [
        pytest.param(
            (
                (1, 1_700_000_000_000, 1_700_000_300_000, Decimal("100.0"), Decimal("101.0")),
                (2, 1_700_000_600_000, 1_700_000_900_000, Decimal("101.0"), Decimal("102.0")),
            ),
            id="identical_trade_lists_have_no_divergences",
        ),
        pytest.param((), id="empty_lists_no_divergences"),
    ],
)
def test_matching_trade_lists_have_no_divergences(trades: tuple[tuple[int, int, int, Decimal, Decimal], ...]) -> None:
    from app.services.lean_sidecar_compare_service import reconcile_trade_lists

    left = [_trade(*args) for args in trades]
    right = [_trade(*args) for args in trades]
    result = reconcile_trade_lists(left_trades=left, right_trades=right)
    assert result.divergences == [], "no divergences"
    assert result.first_divergence_ms_utc is None, "no first divergence"


_TWO_TRADES = (
    (1, 1_700_000_000_000, 1_700_000_300_000, Decimal("100.0"), Decimal("101.0")),
    (2, 1_700_000_600_000, 1_700_000_900_000, Decimal("101.0"), Decimal("102.0")),
)


@pytest.mark.parametrize(
    ("left_count", "divergence_count"),
    [
        pytest.param(1, 1, id="decision_mismatch_when_one_side_has_extra_trade"),
        pytest.param(0, 2, id="one_empty_side_yields_decision_mismatches"),
    ],
)
def test_a_trade_only_one_side_made_is_a_decision_mismatch(left_count: int, divergence_count: int) -> None:
    from app.services.lean_sidecar_compare_service import reconcile_trade_lists

    left = [_trade(*args) for args in _TWO_TRADES[:left_count]]
    right = [_trade(*args) for args in _TWO_TRADES]
    result = reconcile_trade_lists(left_trades=left, right_trades=right)
    assert len(result.divergences) == divergence_count, "one divergence per trade the left side lacks"
    assert all(d.category == "DECISION_MISMATCH" for d in result.divergences), "each is a DECISION_MISMATCH"
    assert result.first_divergence_ms_utc is not None, "a first divergence is reported"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("entry_ms_utc", 1_700_000_000_001, id="one_millisecond_timestamp_shift_entry_ms_utc"),
        pytest.param("exit_ms_utc", 1_700_000_300_001, id="one_millisecond_timestamp_shift_exit_ms_utc"),
        pytest.param("is_synthetic_exit", True, id="synthetic_exit_difference"),
    ],
)
def test_a_changed_decision_field_is_a_decision_mismatch(field: str, value: object) -> None:
    from app.services.lean_sidecar_compare_service import reconcile_trade_lists

    left = [_trade(1, 1_700_000_000_000, 1_700_000_300_000, Decimal("100.0"), Decimal("101.0"))]
    right = [dict(left[0], **{field: value})]

    result = reconcile_trade_lists(left_trades=left, right_trades=right)

    assert [divergence.category for divergence in result.divergences] == ["DECISION_MISMATCH"]


@pytest.mark.parametrize(
    ("left", "right", "reconcile_kwargs", "category", "absent_categories"),
    [
        pytest.param(
            (Decimal("100.0"), Decimal("101.0"), Decimal("10")),
            (Decimal("100.10"), Decimal("101.00"), Decimal("10")),
            (("fill_price_atol", Decimal("0.01")),),
            "FILL_PRICE_DRIFT",
            (),
            id="fill_price_drift_classified",
        ),
        pytest.param(
            (Decimal("100.0"), Decimal("101.0"), Decimal("10")),
            (Decimal("100.0"), Decimal("101.0"), Decimal("20")),
            (),
            "QUANTITY_MISMATCH",
            (),
            id="quantity_mismatch_classified",
        ),
        # Signed quantities with opposite sign on same trade number → DIRECTION_MISMATCH.
        # Should NOT also emit QUANTITY_MISMATCH for the same pair since abs magnitudes match.
        pytest.param(
            (Decimal("100.0"), Decimal("101.0"), Decimal("10")),
            (Decimal("100.0"), Decimal("101.0"), Decimal("-10")),
            (),
            "DIRECTION_MISMATCH",
            ("QUANTITY_MISMATCH",),
            id="direction_mismatch_classified",
        ),
    ],
)
def test_a_differing_fill_is_classified_by_what_differs(
    left: tuple[Decimal, Decimal, Decimal],
    right: tuple[Decimal, Decimal, Decimal],
    reconcile_kwargs: tuple[tuple[str, Decimal], ...],
    category: str,
    absent_categories: tuple[str, ...],
) -> None:
    from app.services.lean_sidecar_compare_service import reconcile_trade_lists

    left_trades = [_trade(1, 1_700_000_000_000, 1_700_000_300_000, left[0], left[1], qty=left[2])]
    right_trades = [_trade(1, 1_700_000_000_000, 1_700_000_300_000, right[0], right[1], qty=right[2])]
    result = reconcile_trade_lists(left_trades=left_trades, right_trades=right_trades, **dict(reconcile_kwargs))
    assert any(d.category == category for d in result.divergences), f"{category} is emitted"
    assert not any(d.category in absent_categories for d in result.divergences), "categories that must not be emitted"


def test_first_divergence_ms_is_earliest() -> None:
    from app.services.lean_sidecar_compare_service import reconcile_trade_lists

    left = [
        _trade(1, 1_700_000_000_000, 1_700_000_300_000, Decimal("100.0"), Decimal("101.0")),
        _trade(2, 1_700_001_000_000, 1_700_001_300_000, Decimal("102.0"), Decimal("103.0")),
    ]
    right = [
        _trade(1, 1_700_000_000_000, 1_700_000_300_000, Decimal("100.05"), Decimal("101.05")),  # drift at t=1.7B
        _trade(2, 1_700_001_000_000, 1_700_001_300_000, Decimal("102.05"), Decimal("103.05")),  # drift at t=1.7B+1M
    ]
    result = reconcile_trade_lists(left_trades=left, right_trades=right, fill_price_atol=Decimal("0.01"))
    assert result.first_divergence_ms_utc == 1_700_000_000_000


@pytest.mark.parametrize(
    ("left_fee", "right_fee", "reconcile_kwargs", "emitted"),
    [
        # Fee difference > commission_atol and assert_fees=True → COMMISSION_DRIFT.
        pytest.param(
            Decimal("1.00"),
            Decimal("1.05"),
            (("commission_atol", Decimal("0.001")), ("assert_fees", True)),
            True,
            id="commission_drift_emitted_when_assert_fees_true",
        ),
        # Fee difference is ignored when assert_fees=False (Branch B fixture).
        pytest.param(
            Decimal("1.00"),
            Decimal("5.00"),
            (("assert_fees", False),),
            False,
            id="commission_drift_suppressed_when_assert_fees_false",
        ),
        # If either side lacks a fee field, COMMISSION_DRIFT is skipped even with assert_fees=True.
        # No fee key in either trade.
        pytest.param(
            None,
            None,
            (("assert_fees", True),),
            False,
            id="commission_drift_suppressed_when_fee_field_absent",
        ),
    ],
)
def test_commission_drift_follows_assert_fees_and_the_fee_field(
    left_fee: Decimal | None,
    right_fee: Decimal | None,
    reconcile_kwargs: tuple[tuple[str, Decimal | bool], ...],
    emitted: bool,
) -> None:
    from app.services.lean_sidecar_compare_service import reconcile_trade_lists

    left = [_trade(1, 1_700_000_000_000, 1_700_000_300_000, Decimal("100.0"), Decimal("101.0"), fee=left_fee)]
    right = [_trade(1, 1_700_000_000_000, 1_700_000_300_000, Decimal("100.0"), Decimal("101.0"), fee=right_fee)]
    result = reconcile_trade_lists(left_trades=left, right_trades=right, **dict(reconcile_kwargs))
    assert any(d.category == "COMMISSION_DRIFT" for d in result.divergences) is emitted


def test_pnl_drift_classified() -> None:
    """PnL divergence beyond propagated tolerance → PNL_DRIFT."""
    from app.services.lean_sidecar_compare_service import reconcile_trade_lists

    # Same prices but PnL manually set to diverge far beyond propagated atol
    qty = Decimal("10")
    fill_price_atol = Decimal("0.01")
    # propagated_atol = qty * 2 * fill_price_atol = 0.20
    # left_pnl intentionally set 1.00 higher than right_pnl (well above 0.20)
    left_trade = {
        "trade_number": 1,
        "entry_ms_utc": 1_700_000_000_000,
        "exit_ms_utc": 1_700_000_300_000,
        "entry_price": Decimal("100.0"),
        "exit_price": Decimal("101.0"),
        "quantity": qty,
        "pnl": Decimal("11.00"),  # expected is 10.00; diff = 1.00 > 0.20
        "signal_reason": "test",
        "is_synthetic_exit": False,
    }
    right_trade = {
        "trade_number": 1,
        "entry_ms_utc": 1_700_000_000_000,
        "exit_ms_utc": 1_700_000_300_000,
        "entry_price": Decimal("100.0"),
        "exit_price": Decimal("101.0"),
        "quantity": qty,
        "pnl": Decimal("10.00"),
        "signal_reason": "test",
        "is_synthetic_exit": False,
    }
    result = reconcile_trade_lists(
        left_trades=[left_trade],
        right_trades=[right_trade],
        fill_price_atol=fill_price_atol,
    )
    assert any(d.category == "PNL_DRIFT" for d in result.divergences)


def test_divergence_dto_uses_decimal_fields() -> None:
    """DivergenceDto.left_fill_price and left_quantity are Decimal, not float."""
    from decimal import Decimal

    from app.services.lean_sidecar_compare_service import reconcile_trade_lists

    left = [_trade(1, 1_700_000_000_000, 1_700_000_300_000, Decimal("100.0"), Decimal("101.0"), qty=Decimal("10"))]
    right = [_trade(1, 1_700_000_000_000, 1_700_000_300_000, Decimal("100.50"), Decimal("101.0"), qty=Decimal("20"))]
    result = reconcile_trade_lists(left_trades=left, right_trades=right, fill_price_atol=Decimal("0.01"))

    price_divs = [d for d in result.divergences if d.category == "FILL_PRICE_DRIFT"]
    assert len(price_divs) >= 1
    d = price_divs[0]
    assert isinstance(d.left_fill_price, Decimal)
    assert isinstance(d.right_fill_price, Decimal)

    qty_divs = [d for d in result.divergences if d.category == "QUANTITY_MISMATCH"]
    assert len(qty_divs) >= 1
    assert isinstance(qty_divs[0].left_quantity, Decimal)
    assert isinstance(qty_divs[0].right_quantity, Decimal)
