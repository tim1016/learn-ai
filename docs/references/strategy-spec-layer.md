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
