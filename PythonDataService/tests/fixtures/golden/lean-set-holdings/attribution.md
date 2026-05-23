# LEAN `SetHoldings` sizing — golden fixture

## What

`entries.json` is every from-flat `SetHoldings(SPY, 1.0)` entry order LEAN
placed during one pinned backtest. Each record is the reconciliation input
→ output for the share-sizing port:

- `tpv` — total portfolio value when the order was sized (cash only; the
  strategy is flat between trades, so portfolio value == cash)
- `price` — the fill price LEAN sized against
- `order_fee` — LEAN's per-order fee for that fill
- `lean_qty` — the share quantity LEAN's `SetHoldings` chose

## Reference

- **Construct**: LEAN `QCAlgorithm.SetHoldings` →
  `IBuyingPowerModel.GetMaximumOrderQuantityForTargetBuyingPower`.
- **LEAN image digest**: `sha256:97884667be20077925996ac22b5e3e16e3a47e7363e01795151459d16786247c`
- **Algorithm**: the EMA-crossover trusted sample
  (`app/lean_sidecar/trusted_samples/ema_crossover.py`), `SetHoldings(SPY, 1.0)`.
- **Run**: cross-engine parity-matrix cell `SPY_W6mo_2025-11-03_to_2026-04-30`
  (LEAN sidecar run `regen-spy-1674d5ef5d87`), captured 2026-05-22.
- **Source of LEAN orders**: that run's `MyAlgorithm-order-events.json`,
  cash trajectory reconstructed from $100,000 start across all fills.

## Pinned sizing law

Every one of the 20 entries is reproduced exactly by:

```
qty = floor( (tpv * (1 - FreePortfolioValuePercentage) - order_fee) / price )
```

with `FreePortfolioValuePercentage = 0.0025` — LEAN's documented default
free-portfolio-value buffer. This is the `lean_set_holdings` sizing model
ported in `app/engine/execution/sizing.py`; the parity test
`tests/engine/execution/test_sizing.py` asserts exact reproduction.

## Regeneration

Regenerated only on deliberate trigger. A commit changing `entries.json`
must re-pin the LEAN run it was derived from and explain why.
