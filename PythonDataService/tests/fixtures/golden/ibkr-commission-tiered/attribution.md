# IBKR US-equity fixed-tier commission — golden fixture

## What

`cases.json` encodes IBKR's published **US equities, Fixed pricing tier**
commission schedule as `(quantity, fill_price) → expected_fee` rows. The
expected fees are derived **by hand from the published schedule**, not from
`IbkrEquityCommissionModel`, so the fixture is an independent ground truth
for the model rather than a circular snapshot of it.

Each case exercises one regime of the schedule:

- **min-fee floor** — raw per-share charge below the $1.00 per-order minimum.
- **per-share rate** — raw per-share above the floor and below the cap.
- **max-pct cap** — 0.5%-of-trade-value cap dominates (low price, high qty).
- **SPY small / SPY large** — the bot's actual symbol at the floor and at the
  per-share rate.

## Schedule (Fixed tier, US equities)

| Component | Value |
|---|---|
| Per share | $0.005 |
| Minimum per order | $1.00 |
| Maximum per order | 0.5% of trade value |
| Rounding | half-up to cents |

`fee = min( max(|qty| × $0.005, $1.00), |qty| × price × 0.5% )`, each term
rounded half-up to cents.

## Reference

- **Source**: Interactive Brokers — *Stocks, ETFs (ETPs) and Warrants —
  US — Fixed* pricing tier (https://www.interactivebrokers.com/en/pricing/commissions-stocks.php).
- **Retrieved**: 2026-05-29.
- **Canonical implementation**: `app/research/parity/ibkr_commission.py`
  (`IbkrEquityCommissionModel`).
- **Validated against**: this fixture via
  `tests/research/parity/test_ibkr_commission_golden.py`.

## Regenerate

Hand-maintained from the published schedule above. Recompute the five
`expected_fee` values with the formula and update `cases.json` only if IBKR
changes the published Fixed-tier rates — commit message must cite the
schedule change (`.claude/rules/numerical-rigor.md` fixture lifecycle).
