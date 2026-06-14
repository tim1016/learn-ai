---
id: VCR-0004
severity: P1
status: open
area: strategy-keys
canonical_file: PythonDataService/app/engine/live/run.py:897
reference: PRD §12.1
first_seen: 2026-06-14
last_seen: 2026-06-14
lens: strategy-registry-key-mapping
dedupe_with_F: none
confidence: high
---

## What

The broker deploy dropdown is fed from `GET /api/engine/strategies`, which lists `_STRATEGY_REGISTRY` keys: `ema_crossover`, `sma_crossover`, `daily_sma_crossover`, `rsi_mean_reversion`, `orb`, `deployment_validation`, `ema_crossover_options`, `rsi_range_a`, `rsi_range_b`, `rsi_range_c`. The form submits the selected key verbatim as `strategy_key` and as `start_options.strategy`. The host daemon command builder passes that string verbatim to the runner, which does `import_module(f"app.engine.strategy.algorithms.{args.strategy}")` with no aliasing.

But the algorithm modules in `app/engine/strategy/algorithms/` are: `sma_crossover.py`, `rsi_mean_reversion.py`, `deployment_validation.py`, `spy_ema_crossover.py`, `spy_ema_crossover_options.py`, `spy_orb.py`, `spy_strategy_a/b/c.py`, plus unregistered `buy_and_hold.py` and `spy_vwap_reversion.py`.

Intersection: only `sma_crossover`, `rsi_mean_reversion`, and `deployment_validation` map 1:1. So **7 of 10 advertised strategies** (`ema_crossover`, `daily_sma_crossover`, `orb`, `ema_crossover_options`, `rsi_range_a`, `rsi_range_b`, `rsi_range_c`) will fail at start with `ModuleNotFoundError`. The ledger is already written with the broken key; the runner exits rc=2; a redeploy from the instance console re-submits the same broken key (`broker-start-stop-card.component.ts` re-seeds from `ledger.strategy_key`).

The codebase has a partial workaround in two ADR 0009 hooks — `_lookup_sizing_surface` (run.py:543-546) and `_enforce_explicit_surface_policy` (deploy.py:145-150) — that strip a leading `spy_` so they can resolve the registry row. The actual `import_module` call (run.py:898) has no such workaround. The asymmetry is the smoking gun: someone knew the registry-key / module-name divergence existed and patched the sizing-policy lookups, but not the import path.

## Where

- `PythonDataService/app/routers/engine.py:377-1198` — `_STRATEGY_REGISTRY` keyed by registry names.
- `PythonDataService/app/routers/engine.py:1634-1650` — `list_engine_strategies` emits keys as `name`.
- `Frontend/src/app/components/broker/broker-deploy-form/broker-deploy-form.component.html:38-43` — `<option [value]="s.name">` passes registry key verbatim.
- `Frontend/src/app/components/broker/broker-deploy-form/broker-deploy-form.component.ts:402-425` — sets both `strategy_key` and `start_options.strategy` to the registry key.
- `PythonDataService/app/engine/live/host_daemon.py:625-638` — command builder forwards `request.strategy` verbatim.
- `PythonDataService/app/engine/live/run.py:897-912` — `import_module(f"app.engine.strategy.algorithms.{args.strategy}")` with no aliasing; PascalCase + `Algorithm` class lookup.
- `PythonDataService/app/engine/live/run.py:543-546` — `_lookup_sizing_surface` has `removeprefix("spy_")` workaround (confession).
- `PythonDataService/app/engine/live/deploy.py:145-150` — `_enforce_explicit_surface_policy` repeats the same workaround.

## Why this severity

PRD §7 P1: "UI implies guarantees the backend/runtime does not enforce. Architectural / SoT drift likely to cause incorrect operator decisions."

The deploy dropdown advertises 10 deployable strategies; only 3 actually run. Not P0 because the failure is loud (rc=2 + stderr) at start time — before any orders flow. But it materially breaks the canonical UI flow for 7 of 10 strategies, the ledger is content-addressed to a key that cannot run (so the operator must re-deploy under a new `run_id` after manual correction), and frontend tests pass while production fails (see VCR-0007 for the test-mocks gap).

## Trading impact

Operator selects "Strategy A — EMA-gap + MACD + RSI-range" → fills required fields → clicks Deploy + Start. Dirty-tree check passes; ledger built and persisted with `strategy_key="rsi_range_a"`; host daemon spawns `python -m app.engine.live.run start --strategy rsi_range_a …`. Child exits rc=2 with `[START] could not import strategy module 'rsi_range_a': No module named …`. No trade is placed.

Effects:
- The content-addressed `run_id` is permanent — the operator must re-deploy under a fresh `run_id` after manually editing the ledger to `spy_strategy_a` (the actual module).
- `spy_ema_crossover.spec.json` auto-fill never matches `ema_crossover` either (see VCR-P3-rollup), so the spec path also fails to auto-populate.
- The deploy-dropdown UX silently degrades to "only deployment_validation reliably works", undermining the entire post-PR1 cockpit.

## Reproduction

```bash
# 1. Registry keys:
grep -nE '^[[:space:]]+"[a-z_]+":' PythonDataService/app/routers/engine.py | head -15

# 2. Actual modules:
ls PythonDataService/app/engine/strategy/algorithms/

# 3. Intersection: only sma_crossover, rsi_mean_reversion, deployment_validation match directly.

# 4. Confirm import path has no aliasing:
sed -n '897,912p' PythonDataService/app/engine/live/run.py

# Dynamic confirmation (do NOT run in this audit):
# podman exec polygon-data-service python -c \
#   "from importlib import import_module; import_module('app.engine.strategy.algorithms.ema_crossover')"
# → ModuleNotFoundError
```

## Suggested resolution (NOT auto-applied)

Pick one source of truth and align all surfaces. Two reasonable shapes:

**A. Make the registry key the module name.** Rename `_STRATEGY_REGISTRY` entries to match modules (`spy_ema_crossover`, `spy_orb`, `spy_strategy_a/b/c`, `spy_ema_crossover_options`). Drop the `removeprefix("spy_")` workaround in `run.py:_lookup_sizing_surface` and `deploy.py:_enforce_explicit_surface_policy`. Spec fixtures already follow the module-name convention so VCR-P3-rollup's auto-fill issue resolves as a side effect. Migration: existing ledgers with `strategy_key="deployment_validation"` are unaffected; legacy ledgers from other keys (none in production today) need a one-shot rewrite.

**B. Add a `module_name` field to `StrategyRegistration`.** Keep registry keys as advertised; have `list_engine_strategies` surface `module: str`; deploy + start use the module name in the ledger and runner CLI.

Either way, also:
- Backfill the frontend Vitest mocks so `getEngineStrategies()` returns the SAME `name` values production emits (current mocks use module-name values — see VCR-0007).
- Add a contract test at the Python boundary: `set(s.name for s in /api/engine/strategies) == set(_STRATEGY_REGISTRY.keys())`.
- Add an integration test: `for key in _STRATEGY_REGISTRY: importlib.import_module(f"app.engine.strategy.algorithms.{<resolved-module-name>}")` so this can never regress silently.

## Provenance of the finding

Lens: `strategy-registry-key-mapping` (workflow `wf_def78013-ce4`, structured-finding `registry-key-module-name-mismatch-blocks-deploy`, verified 2/2 by adversarial pass). Confirmed by direct read of registry + module listing + import path.
