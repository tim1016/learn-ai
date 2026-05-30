# SPY 1-min VWAP-band reversion — port notes (PRD-C / PR-K)

**Status:** `reconciled` (decision-level, 2026-05-29) — Python port matches
the QuantConnect reference trade-by-trade at the decision level; residual
fill-price drift is the documented QC-vs-Polygon vendor floor. See
`docs/references/reconciliations/spy-vwap-reversion.md`. Live shadow
onboarding (spec JSON + decision-column publishing) is PR-L.

## What is being ported

A long-only intraday mean-reversion strategy on SPY 1-minute bars: buy when
price dips a `K·σ` band below the session-anchored VWAP, exit when it reverts
to VWAP, flat by 15:55 ET. The shadow occupant of PRD-C's `NoSubmitBrokerAdapter`.

## Reference oracle

- **Source:** QuantConnect Cloud (LEAN) — `references/quantconnect/spy_vwap_reversion/main.py`.
- **Why QC:** the repo already reconciles QC order exports trade-by-trade via
  `app/research/parity/qc_reconciler.py` (Branch-A/B, `atol=1e-9`). The QC algo
  is the authored reference; the Python port must match its orders exactly.
- **Retrieved / authored:** 2026-05-29.

## Pinned formulation (port must mirror EXACTLY)

| Parameter | Value | Notes |
|---|---|---|
| Symbol / resolution | SPY, 1-minute | |
| VWAP | session-anchored, reset at RTH open | `Σ(typical·vol)/Σvol`, `typical=(H+L+C)/3` |
| Distance | `dist = close − vwap` | |
| Sigma | population std (`ddof=0`) of last `LOOKBACK` dist | `LOOKBACK = 30` |
| Bands | `vwap ± K·σ` | `K = 2.0` |
| Entry (long-only) | flat ∧ `close < lower` ∧ in-window ∧ trades_today < cap | fixed `QUANTITY = 100` |
| Exit | long ∧ `close ≥ vwap` | revert to fair value |
| Session filter (entries) | skip first 5 min + last 5 min before force-flat | `SKIP_OPEN_MIN = SKIP_CLOSE_MIN = 5` |
| Force-flat | 15:55 ET liquidate | |
| Max trades / day | 4 | |
| Data normalization | RAW | matches our engine's price basis |

Fixed share quantity (not `SetHoldings %`) is deliberate: it removes
cash-buffer rounding so the port reproduces the share count exactly.

## How to run it on QuantConnect Cloud and export the orders

1. **Log in** at <https://www.quantconnect.com> → **Create New Algorithm** (Python).
2. **Paste** the contents of `references/quantconnect/spy_vwap_reversion/main.py`
   into `main.py`, replacing the template. (The class is already a `QCAlgorithm`.)
3. **Confirm the backtest window** in `initialize` — it's pinned to
   `2024-03-04 → 2024-03-08` (a quiet RTH week, no SPY splits/dividends). Change
   only if you want a different window; if you do, tell me so I capture the same
   window for the Polygon price-history fixture.
4. **Backtest** (the ▶ button). SPY minute data is included in QC's free tier.
5. When it finishes, open the **Orders** tab of the backtest result. Export the
   orders:
   - Easiest: the backtest result page → **Orders** → there's a download/export
     control; save the orders as JSON.
   - Or via the QC API `/backtests/orders/read` (the shape `qc_reconciler`
     already parses — see its `_parse_qc_orders` docstring).
6. **Send me** (a) the exported orders JSON and (b) the exact start/end dates and
   the SPY data resolution used. That's everything I need to:
   - capture the matching SPY 1-min price history from Polygon as the fixture,
   - finish the Python port + the two indicators (`SessionAnchoredVwap`,
     `RollingDistanceSigma`) with their own golden fixtures,
   - run `qc_reconciler` to prove trade-by-trade parity, and
   - write the `spy_vwap_reversion.spec.json` (shadow, `clientId=43`).

## Open items

- [x] QC orders export captured → `tests/fixtures/golden/spy-vwap-reversion-qc/`
- [x] 1-min SPY price-history fixture (committed LEAN minute cache, 5 sessions)
- [x] `SessionAnchoredVwap` + `RollingDistanceSigma` indicators + parity tests
- [x] `SpyVwapReversionAlgorithm` Python port
- [x] `qc_reconciler` parity: decision-level exact; fill drift within documented
      data-source floor (`docs/references/reconciliations/spy-vwap-reversion.md`)
- [x] Documented tolerance + the 2 confirmed data-source divergences
- [ ] **(PR-L)** `spy_vwap_reversion.spec.json` (submit_mode=shadow, clientId=43,
      decision_columns) — needs a generalized `DecisionSnapshot` (the current
      one is EMA-shaped: ema5/ema10/rsi). The port produces correct *trades*
      today; publishing VWAP *decision columns* for the live shadow run is
      onboarding work.
- [ ] **(PR-L)** Register under `ProcessRegistry`; shadow smoke run via
      `NoSubmitBrokerAdapter` (PR-J, merged).
