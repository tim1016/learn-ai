"""Tests for lean_sidecar_compare_service.reconcile_trade_lists."""

from __future__ import annotations


def _trade(
    n: int,
    entry_ms: int,
    exit_ms: int,
    entry_price: float,
    exit_price: float,
    qty: float = 10,
) -> dict:
    return {
        "trade_number": n,
        "entry_ms_utc": entry_ms,
        "exit_ms_utc": exit_ms,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "quantity": qty,
        "pnl": (exit_price - entry_price) * qty,
        "signal_reason": "test",
        "is_synthetic_exit": False,
    }


def test_identical_trade_lists_have_no_divergences() -> None:
    from app.services.lean_sidecar_compare_service import reconcile_trade_lists

    trades = [
        _trade(1, 1_700_000_000_000, 1_700_000_300_000, 100.0, 101.0),
        _trade(2, 1_700_000_600_000, 1_700_000_900_000, 101.0, 102.0),
    ]
    result = reconcile_trade_lists(left_trades=trades, right_trades=trades)
    assert result.divergences == []
    assert result.first_divergence_ms_utc is None


def test_decision_mismatch_when_one_side_has_extra_trade() -> None:
    from app.services.lean_sidecar_compare_service import reconcile_trade_lists

    left = [_trade(1, 1_700_000_000_000, 1_700_000_300_000, 100.0, 101.0)]
    right = [
        _trade(1, 1_700_000_000_000, 1_700_000_300_000, 100.0, 101.0),
        _trade(2, 1_700_000_600_000, 1_700_000_900_000, 101.0, 102.0),
    ]
    result = reconcile_trade_lists(left_trades=left, right_trades=right)
    assert len(result.divergences) >= 1
    assert any(d.category == "DECISION_MISMATCH" for d in result.divergences)
    assert result.first_divergence_ms_utc is not None


def test_fill_price_drift_classified() -> None:
    from app.services.lean_sidecar_compare_service import reconcile_trade_lists

    left = [_trade(1, 1_700_000_000_000, 1_700_000_300_000, 100.0, 101.0)]
    right = [_trade(1, 1_700_000_000_000, 1_700_000_300_000, 100.10, 101.00)]
    result = reconcile_trade_lists(left_trades=left, right_trades=right, fill_price_atol=0.01)
    assert any(d.category == "FILL_PRICE_DRIFT" for d in result.divergences)


def test_first_divergence_ms_is_earliest() -> None:
    from app.services.lean_sidecar_compare_service import reconcile_trade_lists

    left = [
        _trade(1, 1_700_000_000_000, 1_700_000_300_000, 100.0, 101.0),
        _trade(2, 1_700_001_000_000, 1_700_001_300_000, 102.0, 103.0),
    ]
    right = [
        _trade(1, 1_700_000_000_000, 1_700_000_300_000, 100.05, 101.05),  # drift at t=1.7B
        _trade(2, 1_700_001_000_000, 1_700_001_300_000, 102.05, 103.05),  # drift at t=1.7B+1M
    ]
    result = reconcile_trade_lists(left_trades=left, right_trades=right, fill_price_atol=0.01)
    assert result.first_divergence_ms_utc == 1_700_000_000_000


def test_quantity_mismatch_classified() -> None:
    from app.services.lean_sidecar_compare_service import reconcile_trade_lists

    left = [_trade(1, 1_700_000_000_000, 1_700_000_300_000, 100.0, 101.0, qty=10)]
    right = [_trade(1, 1_700_000_000_000, 1_700_000_300_000, 100.0, 101.0, qty=20)]
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
        _trade(1, 1_700_000_000_000, 1_700_000_300_000, 100.0, 101.0),
        _trade(2, 1_700_000_600_000, 1_700_000_900_000, 101.0, 102.0),
    ]
    result = reconcile_trade_lists(left_trades=left, right_trades=right)
    assert len(result.divergences) == 2
    assert all(d.category == "DECISION_MISMATCH" for d in result.divergences)
