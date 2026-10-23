# RP-006 — internal research backtest arithmetic

Generated: 2026-10-23

Reference: the platform's own research backtest rule in `app/research/signal/backtest.py`: position at bar `t−1` earns bar `t` return; cost is `cost_bps/10000 × |w_t−w_(t−1)|`; cumulative net return begins at zero. This is an internal policy fixture, not external engine parity.

Regenerate: `python3 scripts/fixture_generators/signal_backtest_hand.py` from the repository root. Outputs are hand-calculated constants in the script; the generator does not call the implementation.

Inputs: two synthetic 3- and 4-bar cases, 100 bps transaction cost and one bar per day for annualized turnover. No timestamps, calendar boundaries, fill model, or commissions are exercised.

Tolerance: `atol=1e-12, rtol=0` for the displayed decimal arithmetic and cumulative sums. The fixture pins the starting zero balance in drawdown and the entry/reversal/flatten cost convention.
