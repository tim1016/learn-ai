# ENG-003 — Trade Statistics

## Evidence Layers

**Layer 1 — Market input provenance:** Synthetic. 3-case grid of 4-trade pnl_pct
sets spanning win/loss mixes. Hand-designed for exact verifiability.
See `engine_stats_extended.py::TRADE_CASES`.

**Layer 2 — Methodology provenance:** Standard portfolio statistics. Profit factor
= gross_win / gross_loss; payoff_ratio = avg_win / |avg_loss|; expectancy = avg_trade.

**Layer 3 — Independent numerical oracle:** Explicit Python formula replicating
the same arithmetic as the canonical, but written from first principles without
calling `compute_trade_statistics`.

## Columns (output)

- total_trades, winning_trades, losing_trades
- win_rate = winning / total
- avg_win_pct = mean of positive pnl_pcts
- avg_loss_pct = mean of negative pnl_pcts (signed: negative number)
- avg_trade_pct = mean of all pnl_pcts
- largest_win_pct = max(pnl_pcts)
- largest_loss_pct = min(pnl_pcts)
- profit_factor = gross_win / gross_loss
- expectancy_pct = avg_trade_pct (expectation of single trade)
- payoff_ratio = avg_win / |avg_loss|

## Tolerance

Integer columns: exact. Float columns: atol=1e-12, rtol=0.0.

## Canonical Implementation

`PythonDataService/app/engine/results/statistics.py::compute_trade_statistics`
Accepts Sequence of _TradeLike (pnl_pcts extracted as float).

## Regeneration

  python scripts/generate_fixtures.py --id ENG-003 --force \
    --justification "<reason>"

## Generation Metadata

Generated: 2026-05-09
Oracle: hand_computed — explicit formula matching canonical arithmetic
Script: scripts/fixture_generators/engine_stats_extended.py
(initial generation)
