# SMA Crossover — Signal Program promotion

## Scope

Issue #1730 (Slice 5 of PRD
`docs/prds/sealed-signal-program-to-governed-alpaca-bot.md`) promotes this
strategy through the governed Signal Program seam: a registry-backed
`SignalProgram`/`SignalSession` with a sealed contract, a pinned semantic
trace root, and a committed build receipt.

It does not change this strategy's decision math. The promotion adds the
custody split (`evaluate_signal_bar` describes an action without mutating
position custody; `commit_signal_decision` owns the effect) and the sealed
identity used by Start/Resume build-proof admission.

- Program version: `sma-crossover/v1`
- Canonical implementation: `app/engine/strategy/algorithms/sma_crossover.py::SmaCrossoverAlgorithm`

## What the trace root is — and what it is not

**It is a regression pin, not cross-engine reference equivalence.**

`golden_trace_root` is a SHA-256 commitment over this program's own ordered
`EvaluationTrace` payloads, produced by replaying the committed cells
through *this* implementation. It detects any semantic change to this
program's decisions — a changed gate, a changed indicator period, a
reordered fact — and it is what build-proof admission verifies the running
bytes against.

It is **not** evidence of agreement with an external reference. This
strategy has no LEAN or TradingView reconciliation; the contract states
that directly in `numerical_provenance.reference`. Read the root as
"this program still decides exactly what it decided when it was sealed",
not as "this program matches an independent implementation". Only
`ema_crossover_signal` in this slice carries a genuine cross-engine
receipt, at
[`reconciliations/ema-crossover-signal-lean-2026-07-18.md`](reconciliations/ema-crossover-signal-lean-2026-07-18.md).

## Validated settings

  - `short_window` = `10`
  - `long_window` = `30`
  - `resolution_minutes` = `15`

## Trace corpus

`PythonDataService/tests/fixtures/golden/sma-crossover-signal/v1/trace-corpus.json`

- Cells: 10, drawn from the committed
  `PythonDataService/tests/fixtures/golden/cross-engine-studies/cells/`
  corpus (Polygon-captured one-minute bars).
- Total traces: 35,900
- Corpus root: `b0a136f7b485179bc37c7998df430480b94b0866d9bc58dbead636fa84a320e9`
- Replay window: `2024-03-28` .. `2026-03-27`, set by the strategy's own
  `initialize()`; `BacktestEngine.iter_bars` bounds every consumer to it.
- Byte-regenerable:
  ```
  python -m scripts.generate_signal_program_trace_corpus \
    --program sma_crossover \
    --output tests/fixtures/golden/sma-crossover-signal/v1/trace-corpus.json --check
  ```
  (run from `PythonDataService/`)

## Tolerance

Not applicable, and deliberately so. `equivalence_level="bit_exact"`
(`tolerance_atol`/`tolerance_rtol` are `None`) because the root is an exact
SHA-256 identity commitment over Decimal-exact trace payloads, not a
floating-point comparison. There is no external series to compare against
at a tolerance — see the section above. Indicator-level tolerances, where
they exist for shared indicators, live with those indicators' own notes.

## Pinned by

`PythonDataService/tests/engine/strategy/test_signal_program_qualification_matrix.py`,
parameterized on `sma_crossover`:

1. `test_validated_settings_corpus_has_a_pinned_trace_root` — replays every
   cell and compares the regenerated root against the sealed one.
2. `test_artifact_digest_matches_its_signal_decision_closure` — the sealed
   digest covers exactly this program's decision-math file closure.
3. `test_exclusion_list_only_names_files_actually_in_the_closure`,
   `test_exclusions_and_artifact_paths_are_disjoint`,
   `test_every_exclusion_carries_a_non_trivial_reason` — the closure's
   exclusions are real, disjoint, and justified.

Discard safety (a `DISCARD` settlement must leave custody untouched) is
pinned for every registered program by
`PythonDataService/tests/engine/strategy/test_signal_program_discard_safety.py`.
