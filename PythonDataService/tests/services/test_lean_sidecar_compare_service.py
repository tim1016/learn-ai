"""Tests for lean_sidecar_compare_service.reconcile_trade_lists."""

from __future__ import annotations

from decimal import Decimal


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


def test_identical_trade_lists_have_no_divergences() -> None:
    from app.services.lean_sidecar_compare_service import reconcile_trade_lists

    trades = [
        _trade(1, 1_700_000_000_000, 1_700_000_300_000, Decimal("100.0"), Decimal("101.0")),
        _trade(2, 1_700_000_600_000, 1_700_000_900_000, Decimal("101.0"), Decimal("102.0")),
    ]
    result = reconcile_trade_lists(left_trades=trades, right_trades=trades)
    assert result.divergences == []
    assert result.first_divergence_ms_utc is None


def test_decision_mismatch_when_one_side_has_extra_trade() -> None:
    from app.services.lean_sidecar_compare_service import reconcile_trade_lists

    left = [_trade(1, 1_700_000_000_000, 1_700_000_300_000, Decimal("100.0"), Decimal("101.0"))]
    right = [
        _trade(1, 1_700_000_000_000, 1_700_000_300_000, Decimal("100.0"), Decimal("101.0")),
        _trade(2, 1_700_000_600_000, 1_700_000_900_000, Decimal("101.0"), Decimal("102.0")),
    ]
    result = reconcile_trade_lists(left_trades=left, right_trades=right)
    assert len(result.divergences) >= 1
    assert any(d.category == "DECISION_MISMATCH" for d in result.divergences)
    assert result.first_divergence_ms_utc is not None


def test_fill_price_drift_classified() -> None:
    from app.services.lean_sidecar_compare_service import reconcile_trade_lists

    left = [_trade(1, 1_700_000_000_000, 1_700_000_300_000, Decimal("100.0"), Decimal("101.0"))]
    right = [_trade(1, 1_700_000_000_000, 1_700_000_300_000, Decimal("100.10"), Decimal("101.00"))]
    result = reconcile_trade_lists(left_trades=left, right_trades=right, fill_price_atol=Decimal("0.01"))
    assert any(d.category == "FILL_PRICE_DRIFT" for d in result.divergences)


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


def test_quantity_mismatch_classified() -> None:
    from app.services.lean_sidecar_compare_service import reconcile_trade_lists

    left = [_trade(1, 1_700_000_000_000, 1_700_000_300_000, Decimal("100.0"), Decimal("101.0"), qty=Decimal("10"))]
    right = [_trade(1, 1_700_000_000_000, 1_700_000_300_000, Decimal("100.0"), Decimal("101.0"), qty=Decimal("20"))]
    result = reconcile_trade_lists(left_trades=left, right_trades=right)
    assert any(d.category == "QUANTITY_MISMATCH" for d in result.divergences)


def test_empty_lists_no_divergences() -> None:
    from app.services.lean_sidecar_compare_service import reconcile_trade_lists

    result = reconcile_trade_lists(left_trades=[], right_trades=[])
    assert result.divergences == []
    assert result.first_divergence_ms_utc is None


def test_one_empty_side_yields_decision_mismatches() -> None:
    from app.services.lean_sidecar_compare_service import reconcile_trade_lists

    left = []
    right = [
        _trade(1, 1_700_000_000_000, 1_700_000_300_000, Decimal("100.0"), Decimal("101.0")),
        _trade(2, 1_700_000_600_000, 1_700_000_900_000, Decimal("101.0"), Decimal("102.0")),
    ]
    result = reconcile_trade_lists(left_trades=left, right_trades=right)
    assert len(result.divergences) == 2
    assert all(d.category == "DECISION_MISMATCH" for d in result.divergences)


def test_direction_mismatch_classified() -> None:
    """Signed quantities with opposite sign on same trade number → DIRECTION_MISMATCH."""
    from app.services.lean_sidecar_compare_service import reconcile_trade_lists

    left = [_trade(1, 1_700_000_000_000, 1_700_000_300_000, Decimal("100.0"), Decimal("101.0"), qty=Decimal("10"))]
    right = [_trade(1, 1_700_000_000_000, 1_700_000_300_000, Decimal("100.0"), Decimal("101.0"), qty=Decimal("-10"))]
    result = reconcile_trade_lists(left_trades=left, right_trades=right)
    assert any(d.category == "DIRECTION_MISMATCH" for d in result.divergences)
    # Should NOT also emit QUANTITY_MISMATCH for the same pair since abs magnitudes match
    assert not any(d.category == "QUANTITY_MISMATCH" for d in result.divergences)


def test_commission_drift_emitted_when_assert_fees_true() -> None:
    """Fee difference > commission_atol and assert_fees=True → COMMISSION_DRIFT."""
    from app.services.lean_sidecar_compare_service import reconcile_trade_lists

    left = [_trade(1, 1_700_000_000_000, 1_700_000_300_000, Decimal("100.0"), Decimal("101.0"), fee=Decimal("1.00"))]
    right = [_trade(1, 1_700_000_000_000, 1_700_000_300_000, Decimal("100.0"), Decimal("101.0"), fee=Decimal("1.05"))]
    result = reconcile_trade_lists(
        left_trades=left,
        right_trades=right,
        commission_atol=Decimal("0.001"),
        assert_fees=True,
    )
    assert any(d.category == "COMMISSION_DRIFT" for d in result.divergences)


def test_commission_drift_suppressed_when_assert_fees_false() -> None:
    """Fee difference is ignored when assert_fees=False (Branch B fixture)."""
    from app.services.lean_sidecar_compare_service import reconcile_trade_lists

    left = [_trade(1, 1_700_000_000_000, 1_700_000_300_000, Decimal("100.0"), Decimal("101.0"), fee=Decimal("1.00"))]
    right = [_trade(1, 1_700_000_000_000, 1_700_000_300_000, Decimal("100.0"), Decimal("101.0"), fee=Decimal("5.00"))]
    result = reconcile_trade_lists(
        left_trades=left,
        right_trades=right,
        assert_fees=False,
    )
    assert not any(d.category == "COMMISSION_DRIFT" for d in result.divergences)


def test_commission_drift_suppressed_when_fee_field_absent() -> None:
    """If either side lacks a fee field, COMMISSION_DRIFT is skipped even with assert_fees=True."""
    from app.services.lean_sidecar_compare_service import reconcile_trade_lists

    # No fee key in either trade
    left = [_trade(1, 1_700_000_000_000, 1_700_000_300_000, Decimal("100.0"), Decimal("101.0"))]
    right = [_trade(1, 1_700_000_000_000, 1_700_000_300_000, Decimal("100.0"), Decimal("101.0"))]
    result = reconcile_trade_lists(
        left_trades=left,
        right_trades=right,
        assert_fees=True,
    )
    assert not any(d.category == "COMMISSION_DRIFT" for d in result.divergences)


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
