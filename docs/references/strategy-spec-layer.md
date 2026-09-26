# Strategy Spec layer — port attribution

## Target
`PythonDataService/app/engine/strategy/spec/` — declarative
``StrategySpec`` schema + ``SpecAlgorithm`` evaluator.

This is **not a port from an external reference**. It is a parity-pinned
secondary implementation of three internal canonical algorithms that
already have their own external references:

| Spec fixture | Hand-coded twin (canonical) | Twin's external reference |
|---|---|---|
| `fixtures/spy_ema_crossover.spec.json` | `app/engine/strategy/algorithms/spy_ema_crossover.py::SpyEmaCrossoverAlgorithm` | LEAN `Algorithm.CSharp/SpyEmaCrossoverAlgorithm.cs` (bit-exact, see `app/engine/tests/test_spy_validation.py`) |
| `fixtures/sma_crossover.spec.json` | `app/engine/strategy/algorithms/sma_crossover.py::SmaCrossoverAlgorithm` | LEAN; rule reimplemented inline in `app/engine/tests/test_sma_crossover_parity.py` |
| `fixtures/rsi_mean_reversion.spec.json` | `app/engine/strategy/algorithms/rsi_mean_reversion.py::RsiMeanReversionAlgorithm` | LEAN; rule reimplemented inline in `app/engine/tests/test_rsi_mean_reversion_parity.py` |

The hand-coded twins are math-authority per `docs/math-sources-of-truth.md`.
The spec layer is parity-pinned secondary; if it ever drifts, the hand-
coded version is the authority and the spec evaluator is the bug.

## Parity contract
For each of the three pinned strategies, ``SpecAlgorithm`` (driven by
the canonical fixture JSON) must produce the **same trade log
trade-by-trade** as the hand-coded twin when both run against the same
synthetic minute-bar stream through the same ``BacktestEngine``
configuration. "Same trade log" means equal:

- entry timestamp, entry price
- exit timestamp, exit price
- PnL points, PnL percent
- WIN / LOSS verdict
- indicator-snapshot values at signal time

Trade count must also match. The parity tests assert all of the above
in `assert_trade_logs_match` — see
`app/engine/strategy/spec/tests/_parity_helpers.py`.

## Tolerance
**Strict equality (zero tolerance).** Both implementations consume the
same indicator instances from `app/engine/indicators/`, so values are
identical bit-for-bit. There is no floating-point reconciliation
window — any drift is a bug in the spec layer.

This is the strongest tolerance level (per
`.claude/rules/numerical-rigor.md` § "Equivalence levels": **Bit-exact**)
and is achievable here because the spec layer reuses the engine's
indicator math rather than reimplementing it. The tests use raw
equality (`==`) on `Decimal` values rather than `np.allclose` — the
contract isn't tolerance-based.

## Why no golden fixture?
Golden fixtures (per `.claude/rules/numerical-rigor.md` § "Golden
fixtures") are appropriate when porting from an external source whose
output we capture once. Here the "reference" is another implementation
inside this repo whose own correctness is pinned by its own tests
(`test_spy_validation.py` for LEAN bit-exactness, indicator parity tests
for the indicators it consumes). Recapturing those outputs as a frozen
fixture would just create a maintenance burden — when the canonical
algorithm changes intentionally, the spec parity test catches it
immediately because they diverge in the same run.

## Test files
- `app/engine/strategy/spec/tests/test_spec_spy_ema_parity.py`
- `app/engine/strategy/spec/tests/test_spec_sma_parity.py`
- `app/engine/strategy/spec/tests/test_spec_rsi_mean_reversion_parity.py`
- `app/engine/strategy/spec/tests/test_spec_round_trip.py` — schema
  validation, JSON Schema export, malformed-spec rejection
- `app/engine/strategy/spec/tests/test_spec_manage_rules.py` —
  Phase 2.1 manage-layer behavior tests (no parity twin; engineered
  scenarios with known answers)

## Lake input authority (#2446)

`app/services/spec_run_data.py::materialize_spec_data_source` admits Spec
and its research-run consumers through the same
`app/data_lake/run_materialization.py::materialize_engine_run` used by
Strategy Lab. Both default to Polygon split-adjusted minute bars and the
regular session, using the shared `resolve_data_roots` and
`LeanMinuteDataReader`. Spec no longer reads `LEAN_DATA_ROOT` or
`LEAN_DATA_CACHE`; those settings and mounts remain in use by prediction
generation and reference qualification, so this change does not remove them.

The materializer owns fetching, catalog coordination and coverage refusal.
Spec retains its additional content admission from #2445: every scheduled
session must contain readable regular-hours bars. Missing, unreadable or
stale-adjustment files refuse explicitly, and zero evaluated bars remain a
failure. Calendar closures are not gaps and early closes are honored.

Successful Spec responses carry `lake_data_availability_hash`. A research
run using the same reader records `lake:<hash>` in the revision component
of its persisted `data_snapshot_id`, after materialization and before
execution. Injected synthetic sources claim no lake fingerprint. The hash
is the lake state admitted for the run, including supporting artifacts;
it is **not** a byte-exact digest of only the bars consumed. Concurrent
replacement between receipt and read is separately tracked in #2455.

`tests/routers/test_spec_strategy_coverage.py` proves exact equality of
every bar actually consumed by Spec and Strategy Lab, including Decimal
OHLCV, symbol and integer UTC clocks, over a QQQ window containing a holiday
and early close. It also covers a lake-held non-SPY symbol with both legacy
environment variables absent (HTTP 500 on the pre-fix implementation), a
cold capture, lake-gate refusals and the existing content checks.
`tests/research/runs/test_runner_inmemory.py` proves that shared callers
record the admitted fingerprint and surface the same coverage refusal.
No indicator, strategy, sizing or fill formula changes in this migration.

## Phase boundaries
The schema admits forward-compatible Phase 2+ shapes
(`OPTION_TEMPLATE`, multi-leg legs, multi-symbol portfolios). The
Phase 1 evaluator refuses to run them with a descriptive
`NotImplementedError`. This keeps the "if it loads, it runs"
contract: every primitive accepted by the schema is one the evaluator
can actually evaluate. `BarField` operands were briefly admitted but
removed once it became clear the Phase 1 evaluator could not run them
(see PR #90 review).

## Authority cross-references
- `docs/math-sources-of-truth.md` § Strategies — declares the spec
  layer as parity-pinned secondary
- `docs/architecture/engine-authority-map.md` — declares the spec
  layer as the canonical owner of "configurable strategy spec" jobs
