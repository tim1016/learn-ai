# Deployment Validation — Signal Program promotion

## Scope

Issue #1730 (Slice 7 of PRD
`docs/prds/sealed-signal-program-to-governed-alpaca-bot.md`) promotes this
strategy through the governed Signal Program seam: a registry-backed
`SignalProgram`/`SignalSession` with a sealed contract, a pinned semantic
trace root, and a committed build receipt. It is the last of the six
programs promoted in that slice, and the only one whose decision clock is
the raw one-minute bar rather than a consolidated bucket.

It does not change this strategy's decision rule — the consecutive-green
pattern, the three-clock hold, and the calendar-derived session window are
documented in
[`deployment-validation-consecutive-green.md`](deployment-validation-consecutive-green.md)
and are unchanged. The promotion adds the custody split
(`evaluate_signal_bar` describes an action without mutating position
custody; `commit_signal_decision` owns the effect) and the sealed identity
used by Start/Resume build-proof admission.

- Program version: `deployment-validation/v1`
- Canonical implementation:
  `app/engine/strategy/algorithms/deployment_validation.py::DeploymentValidationConsecutiveGreen`
- Sole public construction seam:
  `app/engine/strategy/registry.py::_build_deployment_validation_signal_program`

## What the promotion changed in the program itself

Two things, both recorded in the module and method docstrings of the
canonical implementation:

1. **Position custody moved from the fill callback to the commit.** Before
   promotion, `_in_position` and the exit countdown were set in the
   `LONG`-fill branch of `on_order_event`. That is harmless in Backtest,
   where a `next_bar_open` fill always lands before the next bar's own
   evaluation, but the live adapter never calls `on_order_event` at all, so
   a live-deployed bot would have entered once and never exited.
   `commit_signal_decision` now owns the transition for both paths; the
   golden corpus replay confirms no bar's decision changed.
2. **The decision clock is declared, not inferred.** The factory creates the
   session with `timeframe_ms=60_000` because this program decides on every
   raw minute bar through `on_minute_bar`; its one-minute passthrough
   consolidator exists only to retain chart bars. The contract seals the
   same value as `decision_timeframe_ms`.

## What the trace root is — and what it is not

**It is a regression pin, not cross-engine reference equivalence.**

`golden_trace_root` is a SHA-256 commitment over this program's own ordered
`EvaluationTrace` payloads, produced by replaying the committed cells
through *this* implementation. It detects any semantic change to this
program's decisions — a moved barrier, a changed hold length, a reordered
fact — and it is what build-proof admission verifies the running bytes
against.

It is **not** evidence of agreement with an external reference. This
program has no LEAN or TradingView reconciliation; the contract states that
directly in `numerical_provenance.reference`. Read the root as "this program
still decides exactly what it decided when it was sealed", not as "this
program matches an independent implementation". The separate, tolerance-0
session-window parity against the QC shadow copy and LEAN template
(`tests/engine/test_deployment_validation_session_window_parity.py`) is a
parity of the *barrier arithmetic*, not of the decision stream.

## Validated settings

This program is not indicator-driven, so beyond the always-injected signal
symbol there is no tunable the contract pins (`validated_settings={}`).

- `symbol` — one of the validated symbols `AAPL`, `QQQ`, `SPY`, `TSLA`
- `trade_symbol` — defaults to `symbol`; hidden from the deploy form

## Trace corpus

`PythonDataService/tests/fixtures/golden/deployment-validation-signal/v1/trace-corpus.json`

- Cells: 10, drawn from the committed
  `PythonDataService/tests/fixtures/golden/cross-engine-studies/cells/`
  corpus (Polygon-captured one-minute bars).
- Total traces: 585,300 — one per RTH minute rather than one per
  15-minute bucket, which is why this corpus is roughly sixteen times the
  size of the other programs' shared 35,900-trace window.
- Corpus root: `5dca9fde8269386367e9d12b5f22a4caaa11c20d93ae44094e62a91c461d9ce8`
- Replay window: `2024-03-28` .. `2026-04-15`, set by the strategy's own
  `initialize()`; `BacktestEngine.iter_bars` bounds every consumer to it.
  Each cell's data runs to 2026-04-30, so the final ~11 trading days of
  every cell fall outside the window and are not replayed.
- Byte-regenerable:
  ```
  python -m scripts.generate_signal_program_trace_corpus \
    --program deployment_validation \
    --output tests/fixtures/golden/deployment-validation-signal/v1/trace-corpus.json --check
  ```
  (run from `PythonDataService/`)

## Tolerance

Not applicable, and deliberately so. `equivalence_level="bit_exact"`
(`tolerance_atol`/`tolerance_rtol` are `None`) because the root is an exact
SHA-256 identity commitment over Decimal-exact trace payloads, not a
floating-point comparison. There is no external series to compare against
at a tolerance — see the section above.

## Pinned by

`PythonDataService/tests/engine/strategy/test_signal_program_qualification_matrix.py`,
parameterized on `deployment_validation`:

1. `test_validated_settings_corpus_has_a_pinned_trace_root` — replays every
   cell and compares the regenerated root against the sealed one.
2. `test_artifact_digest_matches_its_signal_decision_closure` — the sealed
   digest covers exactly this program's decision-math file closure.
3. `test_exclusion_list_only_names_files_actually_in_the_closure`,
   `test_exclusions_and_artifact_paths_are_disjoint`,
   `test_every_exclusion_carries_a_non_trivial_reason` — the closure's
   exclusions are real, disjoint, and justified.

Program-specific seam behaviour is pinned by
`PythonDataService/tests/engine/strategy/test_deployment_validation_signal_program.py`:
a full ENTER→EXIT cycle with no `on_order_event` delivered, the registry
factory as the single construction seam, discarded ENTER/EXIT/barrier-EXIT
candidates leaving custody untouched, a discarded countdown EXIT re-emitting
on the next eligible clock, and both rollback methods restoring the exact
committed state.

Every cross-program matrix in `tests/engine/strategy/` and
`tests/services/test_signal_program_crash_replay.py` is parameterized off
the registry, so this program is covered by discard safety, session
boundaries, tri-mode trace parity, exit-eligibility and fill-independence,
and crash-window replay on registration rather than by a hand-listed key.
