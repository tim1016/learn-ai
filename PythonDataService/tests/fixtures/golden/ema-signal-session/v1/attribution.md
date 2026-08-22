# EMA SignalSession trace corpus v1

The ten entries name every committed EMA Crossover Signal cell under
`tests/fixtures/golden/cross-engine-studies/cells/` as of 2026-08-21. Each
cell is independently reconciled against the pinned LEAN
`EMA_CROSSOVER_SIGNAL_SOURCE`; this corpus adds the program-version and
settings identity needed to detect a semantic signal-program change.

- Reference: `docs/references/reconciliations/ema-crossover-signal-lean-2026-07-18.md`
- Parameters: each cell's signal symbol (`AAPL`, `QQQ`, `SPY`, or `TSLA`),
  `gap=0.20`, `rsi_min=50`, `rsi_max=70`.
- Root generation: `trace_corpus_root(entries)` in
  `app.engine.strategy.signal_program`, encoded as canonical sorted-key JSON
  and SHA-256.
- Tolerance: not applicable. The root is a byte-stable SHA-256 commitment;
  indicator and trade equivalence retain their separately pinned
  `atol=1e-9, rtol=0` LEAN tests.
