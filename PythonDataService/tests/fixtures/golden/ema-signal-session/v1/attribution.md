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
- Backtest window: `EmaCrossoverSignalAlgorithm.initialize()` sets the
  window itself (`app/engine/strategy/algorithms/ema_crossover_signal.py:161`)
  — `set_start_date(2024, 3, 28)` / `set_end_date(2026, 3, 27)` — while
  every one of these ten cells' own committed price history in
  `tests/fixtures/golden/cross-engine-studies/cells/` runs through
  2026-04-30. This corpus predates
  `scripts/generate_signal_program_trace_corpus.py` and was not built by
  it, but that script reproduces every one of its ten per-cell
  `trace_root`/`trace_count` values exactly (see "Regeneration" below) —
  proof the original replay used this same hardcoded window, not the full
  cell range. The generator itself neither sets nor overrides a window: its
  `_entry_for_cell` hands the cell's minute bars to
  `BacktestEngine(InMemoryDataReader(...))`, whose
  `iter_bars(symbol, start_date, end_date)` bounds iteration to whatever
  window the strategy configured for itself, so roughly the final month of
  each cell is never replayed here either.
- Tolerance: not applicable. The root is a byte-stable SHA-256 commitment;
  indicator and trade equivalence retain their separately pinned
  `atol=1e-9, rtol=0` LEAN tests.

## Regeneration

Regenerated on 2026-08-28 (PR #1865 review) with
`scripts/generate_signal_program_trace_corpus.py`, and byte-regenerable
from this point on. `--check` verifies it in CI alongside every program
promoted from issue #1730.

### Why the hand-authored corpus was retired

This file used to be the one deliberate non-regenerable exception. It was
hand-authored before the generator existed, with the settings written as
submitted (`"0.20"`, `"50"`, `"70"`) rather than as the contract stores
them (`0.2`, `50.0`, `70.0`). Because `trace_corpus_root` hashes each
entry's `settings` text, the aggregate root could not be re-minted — and
that unreproducibility was the evidence that the generator's per-cell
replay was independently authored rather than a tautology.

Review of #1865 found that `gap_bps` — a second entry floor that can
change which bars emit an ENTER — was missing from
`signal_program_settings()`, so two configurations with different floors
produced one `evaluation_id`, which is also the Clerk `decision_id`, the
crash-recovery key, and the receipt identity. Adding it was the fix.

`evaluation_id` is hashed into every `EvaluationTrace`, so that fix moves
**every per-cell `trace_root` in this corpus**, not merely the aggregate.
The hand-authored numbers were therefore invalidated by the correctness
change itself; there was no version of this file that both carried the
fix and preserved the independent-authorship property. Keeping the old
`"0.20"` text while taking the generator's per-cell roots would have kept
the exception test green while the property it documented was already
gone — worse than retiring it honestly.

### Consequence

`golden_trace_root` moves from
`82b81f82b5690919871e50a6c9ac39f26fa28d2c09b96dad4a777d4615cd6179` to
`16044218d7505ab73b632318def91596fae29e9c1d6c4e58c655e9efa4dbf184`, and
the committed build receipt was re-qualified against it. **Any bot sealed
against the old root must be re-sealed**; the admission fence will refuse
it until then. That is the intended behaviour of the fence — the program's
decision identity genuinely changed — but it is an operational step, not
an automatic one.

This regeneration is justified under `.claude/rules/numerical-rigor.md`
("regenerated only with justification"): the reference behaviour changed
because a defect in it was fixed. It is not a re-mint to make a test pass.
