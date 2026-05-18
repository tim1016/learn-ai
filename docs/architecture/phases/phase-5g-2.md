# Phase 5g.2 progress (2026-05-18, separate PR — Engine-Lab cross-run primitive)

The seam that Phase 5g.3 will call from the (currently 501) cross-reconcile endpoint shipped in Phase 5g.1 (PR #271). 5g.2 owns the primitive only; 5g.3 owns the endpoint wire-up and the diff itself.

## What this PR adds

- **New Engine-Lab algorithm** `app/engine/strategy/algorithms/buy_and_hold.py` — `BuyAndHoldStrategy`. Spiritually identical to the LEAN-Lab trusted sample at `app/lean_sidecar/trusted_samples/buy_and_hold.py`: subscribe to one symbol, set holdings = 1.0 on the first received minute bar, hold to end of backtest. This is the canonical golden case for cross-engine reconciliation — same algorithm on both engines, same workspace data, divergences should be limited to fee / fill-model differences that the `DivergenceCategory` taxonomy already classifies.
- **New module** `app/lean_sidecar/cross_runner.py`:
  - `resolve_strategy_class(name)` walks `app.engine.strategy.algorithms.*` and returns the `Strategy` subclass whose `__name__` matches. Per D3 — no auto-derivation, no fuzzy match. Unknown names raise `StrategyNotFoundError` with the sorted list of known classes so the operator's error message is self-guiding.
  - `_instantiate_with_symbol(cls, symbol)` enforces the cross-run constructor convention: strategies must accept a `symbol` kwarg so the cross-runner can pin the LEAN-Lab run's symbol through to the engine's `LeanMinuteDataReader`. Strategies without that kwarg fail with `StrategyIncompatibleError` rather than producing a confusing crash inside `BacktestEngine.run`.
  - `run_engine_lab_on_workspace(workspace_path, strategy_class_name, *, symbol, start_date, end_date, initial_cash)`: the primitive itself. Reads the workspace's `data/` directory (the same staged zips LEAN-Lab consumed — D3 shared staged data), subclass-wraps the resolved strategy to clobber its date/cash defaults with the LEAN-Lab run's pinned values via `super().initialize()` + override, runs the engine, normalizes `OrderEvent`s to `CrossRunOrderEvent`.
  - `CrossRunOrderEvent` mirrors the Phase 3a `NormalizedOrderEvent` shape so the Phase 5g.3 reconciler can diff both sides symmetrically — `ms_utc` (int64), `direction` ("Buy"/"Sell" derived from sign of engine's signed `fill_quantity`), `fill_quantity` (unsigned magnitude — sign lives in `direction`), `fill_price` + `fee` as `Decimal` for cent-exact wire round-trip.

## Why subclass-wrap to pin dates instead of mutating the instance

`BacktestEngine.run()` calls `strategy.initialize()` itself. Anything we set on the instance BEFORE `.run()` gets clobbered. The cross-runner builds a `_CrossRunStrategy(base_class)` whose `initialize` calls `super().initialize()` and THEN sets `start_date / end_date / initial_cash`. The subclass is constructed in-function (closes over the pinned values), and the instance is built via `__new__` + `__dict__` transfer from a symbol-pinned `base_instance` so any constructor-time state (indicators, latches, registered consolidators in the base's `__init__`) is preserved without re-running the constructor.

## What this PR does NOT do

- **The diff** — Phase 5g.3 lands the diff against `DivergenceCategory` plus the `assert_fees=true` Branch-A override per D3.
- **Endpoint wire-up** — Phase 5g.3 replaces the 501 in `POST /runs/{id}/cross-reconcile` with calls into this primitive.
- **Param overrides beyond symbol / dates / cash** — the cross-run contract is the minimal seam. Indicator window sizes, thresholds, etc. land when an actual caller needs them. The constructor-introspection check is already in place so adding more kwargs in future is a non-breaking extension.

## Test surface

`tests/lean_sidecar/test_cross_runner.py` — 10 tests, all green:

- Strategy resolver finds `BuyAndHoldStrategy` and `SmaCrossoverAlgorithm`; rejects unknown names with a `StrategyNotFoundError` carrying the known-list; refuses to match the `Strategy` abstract base.
- Symbol-kwarg compatibility check predicate is exercised on a hand-built no-symbol Strategy subclass.
- End-to-end cross-run: stage 10 synthetic minute bars in a tmp workspace via `stage_minute_bars`, run `BuyAndHoldStrategy`, confirm exactly one `Buy` event is emitted with `ms_utc` as int, `fill_quantity` as unsigned magnitude, `fill_price` + `fee` as `Decimal`, and the trading-date alignment lands on the staged day.
- Buy-and-hold latch sanity: subsequent bars do not generate additional `Buy` events (`_invested` latch holds).
- Workspace without `data/` directory raises `WorkspaceDataMissingError` (fail-fast — silently producing zero events would mask a real bug).
- Unknown strategy class propagates `StrategyNotFoundError` through the primitive.
- `initial_cash=$50000` override round-trips into the result, proving the subclass-wrap pinning works for non-default values.

## Build sequence

The remaining Phase 5g slices (per `phase-5g.md`):

3. **Phase 5g.3** — diff against `DivergenceCategory`; honor `assert_fees` Branch-A semantics; wire the Phase 5g.1 endpoint to call `run_engine_lab_on_workspace` and return the report.
4. **Phase 5g.4** — frontend UI ("Cross-engine reconcile" button + report panel).
