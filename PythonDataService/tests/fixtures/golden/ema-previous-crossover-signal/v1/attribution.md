# EMA Previous Crossover SignalSession trace corpus v1

The ten entries name every committed cell under
`tests/fixtures/golden/cross-engine-studies/cells/` as of 2026-10-05, replayed
through `EmaPreviousCrossoverAlgorithm`'s Signal Program (#2902). Each cell's
raw minute bars (`lean/observations.csv`) are reused as priced-data input.

- Reference: this corpus is a self-consistency pin of the program's decision
  identity, not a cross-engine claim. The cross-engine evidence for this
  program is the ENG-010 LEAN fixture
  (`tests/fixtures/golden/strategy-parity/ENG-010/v1/`), which pins LEAN's
  own run of the trusted template on the SPY and QQQ six-month cells.
- Parameters: each cell's signal symbol (`AAPL`, `QQQ`, `SPY`, or `TSLA`) at
  the registered validated point: `gap=0.2`, `gap_bps=0.0`, `rsi_min=50.0`,
  `rsi_max=70.0`, `fast_period=5`, `slow_period=10`, `hold_bars=5`,
  `cross_max_age_bars=1`.
- Regenerated: 2026-10-05, after the owner replaced the entry rule the same
  day it was first built (program version `ema-previous-crossover/v2`: a
  cross at most `cross_max_age_bars` 15-minute bars old by the clock, with
  nothing else remembered between bars). No bot or accepted validation ran
  on the first rule.
- Generated: 2026-10-05, by
  `python -m scripts.generate_signal_program_trace_corpus --program ema_previous_crossover --output tests/fixtures/golden/ema-previous-crossover-signal/v1/trace-corpus.json`
- Root generation: `trace_corpus_root(entries)` in
  `app.engine.strategy.signal_program`, encoded as canonical sorted-key JSON
  and SHA-256.
- Backtest window: `EmaPreviousCrossoverAlgorithm.initialize()` sets
  `set_start_date(2024, 3, 28)` / `set_end_date(2026, 3, 27)`, the window
  every other program's corpus replays, and the generator does not override
  it. All programs therefore replay the same bars of the same ten cells.
- Decision identity: the replay commits every staged decision
  (`BacktestEngine.for_decision_identity`). Whether the cross is recent is
  part of each trace (`relation_facts.cross_recent`), so the root pins which
  bars had a recent cross, not only which bars entered.
