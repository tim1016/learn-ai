# Strategy A/B/C self-equivalence (pre-port receipt)

## Target

Strategy A, B, and C — `app/engine/strategy/algorithms/spy_strategy_{a,b,c}.py`,
all extending the shared `RsiRangeStrategy` base
(`app/engine/strategy/algorithms/_rsi_range_base.py`). All three are
long-only, 15-minute RTH SPY strategies sharing an RSI(14) range filter and
an ADX(14) exit gate, differing only in their extra entry gate:

- **A**: EMA(20)−EMA(50) gap > threshold, MACD(12,26,9) > 0.
- **B**: Supertrend(10, 3) long, ADX > entry threshold, MACD > 0.
- **C**: ADX > entry threshold and rising bar-over-bar.

## Reference

None external. `reference_kind=internal_regression` — this fixture does not
certify Strategy A/B/C's math is correct against any outside authority. It
exists per [issue #1699](https://github.com/tim1016/learn-ai/issues/1699) as
a **refactor-neutrality receipt**: Strategy A/B/C were originally written for
manual Strategy Lab use, not bot deployment (see the parent PRD, issue
#1697), and were ported to `SignalIntent` emission in
[issue #1700](https://github.com/tim1016/learn-ai/issues/1700) (S3) so they
could be deployed via the same execution boundary as SMA Crossover / RSI Mean
Reversion. This fixture pinned their *pre-port* trade-log output so that S3
port could prove — mechanically, not in prose — that swapping direct
`set_holdings`/`liquidate` calls for `emit_signal_intent(ENTER/EXIT)` left
every trade unchanged. `PythonDataService/tests/fixtures/test_strategy_parity_fixtures.py`
passed unchanged before and after the #1700 port.

## Protocol

`scripts/fixture_generators/strategy_abc_self_equivalence.py` builds one
seeded synthetic 15-minute SPY-like bar series
(`numpy.random.default_rng(seed=20260820)`, 900 bars, random-walk with
`N(0, 1.35)` per-bar steps off a $400 base) and runs each of
`SpyStrategyAAlgorithm()`, `SpyStrategyBAlgorithm()`, `SpyStrategyCAlgorithm()`
— constructed with **zero overrides**, i.e. their registered default
parameters (`app/engine/strategy/registry.py`
`spy_strategy_{a,b,c}` entries) — through `BacktestEngine`
(`FillModel(mode=SIGNAL_BAR_CLOSE, commission_per_order=0)`). Each strategy's
public `strategy.trade_log` (`list[LoggedTrade]`) is captured verbatim.

This is a deliberate departure from the existing
`app/engine/tests/test_strategies_abc.py`, which drives each strategy's
private `_entry_extra_gate_passes` method directly and never runs the
backtest engine or observes `trade_log`. That file remains useful for
pinning gate-wiring logic in isolation; this fixture pins the end-to-end
observable a port must reproduce.

Golden fixture `ENG-008` under
`PythonDataService/tests/fixtures/golden/strategy-parity/ENG-008/v1/` commits
the input bar series (`input.arrow`) and the resulting trade logs
(`output.json`, one list per strategy — 37 trades for A, 11 for B, 7 for C at
generation). `PythonDataService/tests/fixtures/test_strategy_parity_fixtures.py`
replays the pinned bars through fresh strategy instances and diffs every
`LoggedTrade` field against the pinned output.

## Tolerance

`atol=0, rtol=0` — bit-exact. Every `LoggedTrade` field is `Decimal`, `int`,
`str`, or `bool`, produced by exact `Decimal` arithmetic (subtraction for
`pnl_pts`, division under a fixed `decimal` context for `pnl_pct`) replayed
against the same pinned input. Regeneration with the recorded command
reproduces the fixture exactly (verified: two consecutive runs produced
byte-identical `output.json`). A future S3 port that changes even the last
Decimal digit of one trade must fail this test and explain why in its
commit message, per `.claude/rules/numerical-rigor.md`'s "Loosening
tolerances" rule — this receipt is not a candidate for a looser tolerance,
since there is no expected source of legitimate float drift (no external
library, no cross-platform floating-point divergence) to justify one.

## Open items

- This fixture is a **snapshot**, not a correctness proof. It says nothing
  about whether Strategy A/B/C's entry/exit logic is a good trading idea —
  only that a refactor did or did not change it.
- If the S3 port intentionally changes behavior (e.g. fixing a bug found
  during the port), this fixture must be regenerated and the diff explained
  in the commit message — it is a receipt of a decision, not an
  automatically-passing rubber stamp.
- The synthetic series is shared across all three strategies for input
  economy; it was not tuned to be "realistic" SPY data, only to reliably
  trigger each strategy's distinct entry gate at least a handful of times.

## Tests

- `PythonDataService/tests/fixtures/test_strategy_parity_fixtures.py` —
  the equivalence test described above.
- `PythonDataService/tests/fixtures/test_golden_manifest.py` — validates the
  `ENG-008` manifest entry (hashes, schema, active-file presence) on every PR.
- `PythonDataService/app/engine/tests/test_strategies_abc.py` — unchanged;
  continues to pin the per-strategy gate-wiring logic this fixture does not
  cover (indicator warmup edge cases, individual gate rejection reasons).
