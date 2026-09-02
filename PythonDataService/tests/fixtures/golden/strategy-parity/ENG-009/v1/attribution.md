# RSI Mean Reversion LEAN trade fixture

- Reference engine: QuantConnect LEAN source commit
  `261366a7e26ae942df858ab20df4fef8fa07de67`, executed from image
  `sha256:3dd003372f1ef1981b4e80038e3f1c557f1fe414d1be531f485ef870f81a5771`.
- Source receipt: local LEAN sidecar run
  `companion-pg-1ef2ef27539b413a9628`; algorithm source SHA-256
  `f9f1488f1b4e654770973d14aae8c5f6c37e2176a3217fe1be63bfd72e1d644c`;
  input snapshot SHA-256
  `e60e61548e6d30838f0593e772ad8f67c8a891c4b9ff2830b284ccb8a340e7c0`. The run
  is the W3mo cell of
  `docs/references/reconciliations/rsi-mean-reversion-lean-2026-09-01.md`.
- Market input: Polygon/Massive raw SPY minute archives read from the source
  run's own staged workspace, not from `lean-cache`. That run executed in lake
  mode, so the workspace holds the exact archive bytes LEAN consumed while the
  `lean-cache` copies are re-encoded and no longer hash-match the receipt. The
  generator verifies every selected archive against the SHA-256 values recorded
  in the receipt's `staged_zip_sha256`.
- Protocol: SPY raw regular-session minute bars consolidated to 15 minutes,
  $100,000 initial cash, Interactive Brokers equity commission, LEAN
  `SetHoldings` sizing, and the twin's class constants `window=14`,
  `oversold=30`, `overbought=70`.
- Compact window: 2026-02-02 through 2026-02-25. The window *starts* at the
  source run's own start date, so RSI(14) warmup is bar-for-bar identical on
  both sides; only the tail is truncated. The cut falls after the 2026-02-25
  exit, where the strategy is flat — so the Python engine's end-of-algorithm
  liquidation adds no fill the longer LEAN run never made, and final equity
  equals residual cash. The input contains all 6,630 regular-session minute
  bars. The output contains the six LEAN `Filled` order events (three closed
  trades) copied from the independently executed source receipt in that window.
- Generated: 2026-09-02 with
  `PYTHONPATH=. python tests/fixtures/golden/rsi-mean-reversion-lean/generate.py`
  from the `PythonDataService` directory.
- Tolerance: timestamps, directions, quantities, and fees are exact. Fill
  prices and final equity use absolute tolerance `0.000001` with zero relative
  tolerance; the committed fixture currently reproduces both with zero error.
