"""Each trade's own net profit, and the check that a run's trades account for its result (#2821)."""

from __future__ import annotations

import pytest

from app.research.golden_search.trade_net import reconciles, trade_nets


def test_a_trades_net_is_its_pnl_less_its_entry_and_exit_commission() -> None:
    trades = [{"pnl": 12.5}, {"pnl": -3.0}, {"pnl": 0.0}]
    assert trade_nets(trades, 1.25) == pytest.approx([10.0, -5.5, -2.5], abs=1e-9, rel=0)
    assert trade_nets(trades, 0.0) == pytest.approx([12.5, -3.0, 0.0], abs=1e-9, rel=0)


def test_trades_reconcile_with_their_run_within_a_cent() -> None:
    nets = [10.0, -5.5]
    assert reconciles(nets, 4.5)
    # Exactly one cent apart, either way, still reconciles; two cents does not.
    assert reconciles(nets, 4.51) and reconciles(nets, 4.49)
    assert not reconciles(nets, 4.52)


def test_a_run_whose_trades_miss_a_loss_does_not_reconcile() -> None:
    # A position still open when the window ended moves net profit but is no trade.
    assert not reconciles([300.0, -100.0], -50.0)
