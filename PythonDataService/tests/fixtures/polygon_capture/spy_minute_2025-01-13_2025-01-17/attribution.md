# Polygon fixture: SPY 2025-01-13..2025-01-17

**Captured:** 2026-05-20T01:33:14Z via `scripts/regenerate_polygon_fixture.py` (Polygon SDK 1.12.5).
**Trade count discovered:** 2026-05-20 via `scripts/probe_lean_trade_count.py`.

## Why this window

One full RTH trading week (Mon–Fri Jan 13–17, 2025) on SPY. Selected after the prior candidate (Jan 6–10, 2025) produced zero EMA(5)/EMA(10) crossovers under the template's gating set (gap ≥ 0.20 AND 50 ≤ RSI(14) ≤ 70).

The window contains 4,062 raw 1-minute bars (extended hours included; the canonical fetcher filters to RTH at runtime). One closed round-trip trade in regular session — enough to make the LEAN-vs-engine parity test exercise both the indicator-state assertion *and* the trade-equivalence assertion. Without a trade, the test reduces to "0 == 0", which proves data-path equivalence but not decision-path equivalence (the spec's explicit acceptance gate).

## Observed trade

- **Entry:** 2025-01-17 09:45 ET (first consolidated 15-min bar of Friday), fill at $596.12, quantity 167.
- **Exit:** 2025-01-17 11:00 ET (5 consolidated bars later, the EMA template's `EXIT_BARS=5` time stop), fill at $598.16.
- **Net PnL:** +$338.68 on $100,000 starting cash.

The trade landed on day 4 of the week — consistent with the indicator warmup (EMA(10) and RSI(14) on 15-min bars need 10–14 consolidated bars to be ready, then the cross + gap + RSI conjunction had to fire). This is the first window where everything aligned within the strategy's hard rules.

## Regeneration policy

Per `.claude/rules/numerical-rigor.md` § "Golden fixtures": regenerate only when Polygon amends the historical data (caught by `tests/slow/test_polygon_fixture_freshness.py`), and explain why in the regenerating commit message.
