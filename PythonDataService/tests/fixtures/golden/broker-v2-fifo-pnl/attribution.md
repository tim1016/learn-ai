# FIFO P&L golden fixture — broker-v2 bot panel (S0)

## Source

Standard FIFO inventory method (GAAP / IFRS).
Reference: Kieso, Weygandt & Warfield, *Intermediate Accounting* (17e),
Chapter 8 — Inventories: Measurement.

No external software port. The expected outputs in this fixture were derived
by hand calculation following the FIFO accounting algorithm, then verified
by the test suite.

## Fixture date

2026-07-29

## Generation

These fixtures are hand-computed reference values for the closed-form FIFO
algorithm. They do not depend on an external software run. Re-derivation:
apply the FIFO lot rules from the input fills in temporal order and collect
realized P&L per lot closure. See test file for the derivation steps inline.

## Scenarios

1. **simple_round_trip** — BUY 100 @ $10 then SELL 100 @ $12.
   Expected realized: (12 - 10) × 100 = $200.00; open = $0.
2. **partial_close** — BUY 100 @ $10, SELL 60 @ $12.
   Expected realized: (12 - 10) × 60 = $120.00; 40 shares remain open.
3. **multi_lot** — BUY 100 @ $10, BUY 50 @ $11, SELL 120 @ $13.
   FIFO: close 100 @ ($13-$10)=$300 + close 20 @ ($13-$11)=$40 = $340 total.
   Open: 30 shares @ $11.
4. **reversal** — BUY 100 @ $10, SELL 150 @ $12.
   Close 100 long @ ($12-$10)=$200 realized; 50 shares short @ $12.
5. **multi_day** — BUY 100 @ $10 (day 1), BUY 50 @ $11 (day 2),
   SELL 80 @ $13 (day 2).
   FIFO: close 80 @ ($13-$10)=$240; open = 20 @ $10 + 50 @ $11.
6. **no_fees** — all fills have fee=None; fee_total must be None ("not reported").
7. **with_fees** — fills carry explicit fees; fee_total is the sum.

## Tolerance

``atol=1e-9, rtol=0`` — standard accumulated P&L tolerance per
``docs/.claude/rules/numerical-rigor.md``.  The FIFO algorithm is exact
arithmetic (no transcendentals, no accumulation of rounding); 1e-9 is
conservative given that fill prices have at most 2 decimal places.
