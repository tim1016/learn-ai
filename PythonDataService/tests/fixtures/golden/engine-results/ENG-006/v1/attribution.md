# ENG-006 — realized-equity staircase

## Source

The inputs are synthetic closed-trade ledger records. The methodology is the persisted `BacktestTrade.PnL` accounting contract: start with initial cash and add each net closed-trade P&L at the trade's `exit_ms_utc`.

## Independent numerical oracle

`reference_kind=hand_computed`. Expected points were calculated with exact decimal arithmetic without importing `build_realized_equity_envelope`. Equal timestamps are summed into one post-exit point. An exit at the start or end boundary owns that timestamp, so there is no duplicate anchor.

## Cases

- `equal_exit_order`: `$100000.00 + $250.25 - $75.50 = $100174.75`; the zero-P&L exit and terminal anchor retain that value.
- `boundary_folding`: start `$100.00 + $1.25 = $101.25`; middle `$101.25 - $0.25 = $101.00`; end `$101.00 + $1.00 - $0.50 = $101.50`.

## Tolerance

`atol=1e-6, rtol=0`: persisted contract values cross a float transport boundary, while the oracle calculations themselves are exact decimal arithmetic.

## Regeneration

`python PythonDataService/scripts/fixture_generators/realized_equity_staircase.py`
