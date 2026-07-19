# EMA Crossover Signal — LEAN parity validation (2026-07-18)

## Scope

This receipt validates the canonical signal-only strategy
`PythonDataService/app/engine/strategy/algorithms/ema_crossover_signal.py::EmaCrossoverSignalAlgorithm`
against `EMA_CROSSOVER_SIGNAL_SOURCE` in the LEAN sidecar. The latter is the
named LEAN template for the migrated strategy and intentionally reuses the
single executable `EMA_CROSSOVER_SOURCE` body, preventing legacy and migrated
template rules from drifting.

LEAN necessarily trades a concrete subscribed equity. For these tests, Engine
Lab binds the signal-only ENTER/EXIT intents to that same signal symbol. The
Action Plan's live asset selection is an execution-boundary concern and is not
claimed as a LEAN strategy-signal equivalence result.

## Pinned runtime and data contract

- **LEAN image:** `localhost/learn-ai/lean-sandbox@sha256:0b8d4e381b63daaa4cebbea7af294cc5b140793a6fd13f8c9cfd63ef2a2fb24d`
- **Brokerage:** Interactive Brokers, Margin; LEAN `ImmediateFillModel` and
  `InteractiveBrokersFeeModel`; Engine Lab uses the corresponding
  fee-aware `LeanSetHoldingsSizing` and IBKR fee model.
- **Bars:** Polygon-captured, raw, regular-session, one-minute equity bars;
  both engines consolidate to fifteen-minute signal bars.
- **Signal constants:** EMA(5), EMA(10), Wilders RSI(14), EMA gap >= 0.20,
  RSI in [50, 70], exit after five consolidated bars.
- **SPY capture contract:**
  `615fec830b501a0310389de67232497681f70933b09e72b6f931cc8745e2ebe4`
- **QQQ capture contract:**
  `a20fe2d320b22ecd0ee60948f597c062006f42365c0be0e6babb06e27d6155c4`

## Results

| Ticker | Window | Sessions | Minute observations | 15-min states | Filled orders | Result |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| SPY | 2026-02-02 to 2026-04-30 (W3mo) | 62 | 24,180 | 1,598 | 22 | Pass |
| QQQ | 2026-02-02 to 2026-04-30 (W3mo) | 62 | 24,180 | 1,598 | 40 | Pass |
| SPY | 2025-11-03 to 2026-04-30 (W6mo) | 123 | 47,610 | 3,160 | 40 | Pass |
| QQQ | 2025-11-03 to 2026-04-30 (W6mo) | 123 | 47,610 | 3,160 | 66 | Pass |

Every cell passed all three gates with zero gating divergences:

1. **Observations:** exact `ms_utc` and OHLCV equality for every consumed
   minute bar.
2. **State:** exact timestamps, closes, crossover states, and ENTER/EXIT/HOLD
   sequence; EMA and RSI values within absolute tolerance `1e-9`.
3. **Trades:** reconciled LEAN and Engine Lab fills under the repository's
   eight-category divergence taxonomy, including quantity, timing, price, and
   IBKR commission checks.

The committed evidence is the four cell directories under
`PythonDataService/tests/fixtures/golden/cross-engine-studies/cells/`:

- `SPY_W3mo_2026-02-02_to_2026-04-30`
- `QQQ_W3mo_2026-02-02_to_2026-04-30`
- `SPY_W6mo_2025-11-03_to_2026-04-30`
- `QQQ_W6mo_2025-11-03_to_2026-04-30`

Each `manifest.json` pins the source hash, image digest, data-contract hash,
and hashes of the LEAN observations, state, orders, and reconciliation
artifacts. Re-run the four cells with
`PythonDataService/scripts/regenerate_cross_engine_study.py --cell <cell-id>`;
the parity test is
`PythonDataService/tests/research/parity/test_cross_engine_study.py`.
