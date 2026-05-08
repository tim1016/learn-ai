# OPT-IB-002 — Implied Volatility: IBKR Reported vs NR/Brent BSM Solver

**Status:** active · v1 (2026-05-08)
**Category:** options-pricing
**Canonical:** `PythonDataService/app/volatility/solver.py::implied_volatility`

## What this pins

Our Newton-Raphson/Brent BSM IV solver can round-trip the option price that IBKR used to compute its own reported implied volatility. Both the oracle and our solver invert the same price (`modelGreeks.optPrice`), so agreement measures solver inversion accuracy — not model parity.

## Oracle

`vendor_observed` — IBKR TWS API `modelGreeks.impliedVol`. IBKR backs out this IV from `modelGreeks.optPrice` using a proprietary model (discrete dividends, calibration adjustments). The oracle IV is stored for documentation and range-plausibility checks only; it is not used in the primary tolerance comparison.

## Capture Script

```
python scripts/capture_ibkr_snapshot.py [--symbol SPY] [--port 7497]
```

Requires IBKR TWS or IB Gateway with API access enabled. Writes to `scripts/ibkr_snapshots/opt_ib_002/snapshot_YYYYMMDD_HHMMSS.arrow`.

## Fixture Generator

```
python scripts/generate_fixtures.py --id OPT-IB-002 --force \
  --justification "<reason>"
```

Reads the most recently modified snapshot arrow file. Prints the `manifest.json` version entry to paste in after generation.

## Price Alignment

IBKR backs `impliedVol` out of `modelGreeks.optPrice` (not the bid/ask midpoint). Our solver must invert the same price. Using `mid` instead would compare two quantities derived from different prices, producing a call/put divergence fingerprint: calls diverge ~0.06 vol, puts ~0.002 vol (the asymmetry is caused by IBKR discrete-dividend adjustments affecting calls more than puts). The test uses `ibkr_model_price` from `input.arrow`.

## Tolerance

`atol=1e-3, rtol=0.0` — in **price space** (dollar terms), not IV space.

The primary test (`test_solver_iv_matches_ibkr_within_tolerance`) validates solver correctness by round-trip fidelity: the IV our solver returns, fed back into continuous-dividend BSM, must recover `ibkr_model_price` within 1e-3. IBKR IV divergence from our IV is expected (~0.01–0.07 vol) due to IBKR's proprietary model; comparing IVs directly at 1e-3 would always fail.

## Intrinsic Violations

A small number of deep-ITM calls (≤ 15 of ~2300 contracts) return `INTRINSIC_VIOLATION`: IBKR's model price is below BSM intrinsic value. This is a known IBKR model artifact for deep-ITM options near expiry, not a solver failure. The convergence test documents these separately and asserts they stay below the threshold. See `test_solver_converges_on_all_contracts` for the exact count.

## Capture Filters

Applied at snapshot time to avoid degenerate IV comparisons:

| Filter | Threshold |
|--------|-----------|
| Strike range | ±10% of spot |
| Days to expiry | 7 – 90 calendar days |
| Bid | ≥ $0.05 |
| Ask | > 0, ≥ bid |
| IBKR modelGreeks.optPrice | > 0 (model price must be present) |
| IBKR modelGreeks.impliedVol | [0.05, 2.0] |

## Test Suite

```
python -m pytest tests/fixtures/test_ibkr_iv_fixtures.py -v --noconftest
```

Skips automatically if the fixture has not been generated (status=planned). Generate it by running the capture script (requires IBKR Gateway), then the fixture generator.

## Regeneration

When to regenerate: after capturing a new IBKR snapshot to update the oracle to current market conditions, or after a solver algorithm change that changes the output.

```
python scripts/capture_ibkr_snapshot.py
python scripts/generate_fixtures.py --id OPT-IB-002 --force \
  --justification "New IBKR snapshot YYYYMMDD; reason for update"
```

After generation, update `manifest.json` `active_version` and `status`, run the test suite, and commit all changed files together.
