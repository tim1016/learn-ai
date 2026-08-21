# EMA Signal Program / SignalSession — engine tracer bullet

## Scope

Issue #1725 introduces a broker-neutral decision-cycle seam around the
existing canonical EMA crossover strategy. It does not alter EMA, RSI,
consolidation, fill, portfolio, Action Plan, Clerk, account, or broker logic.

The strategy registry is the only public factory for the program. The program
opens one `EmaCrossoverSignalSession`; the session stages a closed 15-minute
decision clock and requires `COMMIT` or `DISCARD` before accepting another.
`UNSETTLED_STAGE` is a closed quarantine result, not an implicit retry.

## Reference and preserved numerical evidence

- External signal reference: pinned LEAN
  `EMA_CROSSOVER_SIGNAL_SOURCE`, documented in
  [the cross-engine reconciliation receipt](reconciliations/ema-crossover-signal-lean-2026-07-18.md).
- Existing data corpus: ten committed cells under
  `PythonDataService/tests/fixtures/golden/cross-engine-studies/cells/`.
- Indicator / signal tolerance: `atol=1e-9`, `rtol=0`; no tolerance changed
  in this work.

## Trace corpus

`PythonDataService/tests/fixtures/golden/ema-signal-session/v1/trace-corpus.json`
names all ten validated EMA settings cells and pins the stable semantic root
`2082cb107beb5a6d189446f6c9c1c1e580adb106e9c8b09d7b155fddfc1a9f1e`.
The root is SHA-256 over canonical sorted-key JSON of the named settings
evidence. It is an exact identity commitment, not a floating-point comparison.

`tests/engine/strategy/test_ema_signal_program.py` proves:

1. registry construction attaches the staged program;
2. an un-settled stage blocks the next decision clock;
3. a discarded countdown EXIT re-emits on its next eligible clock;
4. a trace-semantic change changes the root, while a trace-preserving refactor
   does not.

Backtest commits each stage immediately at its established order-drain seam,
so its existing externally validated numerical and trade behavior is unchanged.
