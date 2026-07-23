# ADR 0002 — Shadow mode is a broker-adapter-level no-submit switch on the same engine, in a separate OS process

**Status:** Accepted 2026-05-28
**Decision drivers:** add a high-frequency intraday strategy (VWAP-band reversion on 1-min SPY) without committing it to live broker submission until reconciled against paper fills.
**Related:** ADR 0001 (substrate), ADR 0003 (topology), historical plan `docs/archive/plans/ibkr-paper-deployment-plan.md` § 16.

## Context

Objective 3 of the design-lock session was a second strategy with higher trade cadence than the SPY EMA(5)/EMA(10)+RSI(14) crossover, so live-vs-sim divergence data accumulates fast. The candidate is a SPY 1-min VWAP-band mean-reversion strategy. The math is net-new and unproven against paper; submitting its orders to IBKR before reconciliation would conflate strategy-math risk with execution-quality risk in the divergence study.

The design question was where, in the live runtime, the "do or do not submit" decision lives. Three shapes were considered:

1. **Same engine, gated by a config flag** at the engine layer (`if broker_submit_enabled: ...`).
2. **A separate `shadow_engine.py`** — a slimmer parallel engine with no IBKR connection at all (or a different bar source).
3. **One engine hosts both strategies** — drop the single-strategy-per-process invariant and run executing EMA + shadow VWAP in one OS process.

Option (2) was rejected on the grounds that it would contaminate the divergence study with a data-source confound (shadow on Polygon vs executing on IBKR) and require porting every hardening improvement twice. Option (3) was rejected because it gives up process isolation between the executing and shadow strategies, expanding blast radius (a shadow bug could poison the executing process's run).

## Decision

**Single `LiveEngine` codepath, mode-neutral.** The submit-vs-no-submit branch lives at the broker-adapter boundary, not in the engine.

Concretely:
- Engine is unchanged in shape. It computes decisions, records `DecisionRow`, asks an `IBrokerAdapter` to place an order, and records `ExecutionRow` from whatever the adapter returns.
- Two adapter implementations exist:
  - `IbkrBrokerAdapter` — `submit_mode = "live_paper"`. Places real orders via `ib_async`, fills come from the broker.
  - `NoSubmitBrokerAdapter` — `submit_mode = "shadow"`. Never calls `ib.placeOrder`. Synthesizes an `ExecutionRow` with `execution_source = "shadow_sim"`, `fill_model = NEXT_BAR_OPEN` (or per-strategy declared), and `source_bar_close_ms` populated from the live bar the engine just consumed.
- Both adapters connect to the same IBKR Gateway for **market data** (shadow needs the same bar stream the executing engine sees, to preserve data-source isolation in the divergence study). Each uses a distinct `clientId` pinned in the strategy spec.
- Each strategy instance runs as its **own OS process** under `host_daemon.py`'s managed-process registry. Processes do not share state in memory; coordination is via the filesystem artifacts only.

### Five invariants that govern shadow

1. **Cold-start verification:** shadow's order namespace (`bot_order_namespace` in the sidecar) must yield **zero open orders and zero executions at the broker**, ever. The cold-start broker cross-check (per Resolution 2 in the archived paper-deployment plan § 16) treats any nonzero result as poisoned state and refuses to continue.
2. **Explicit row typing:** every `ExecutionRow` carries `execution_source ∈ {"broker_fill", "shadow_sim"}`. Mixing in any one parquet is fine; conflating in any one report category is not.
3. **Synthetic-fill provenance:** shadow `ExecutionRow` rows declare `fill_model` and `source_bar_close_ms` explicitly. The fill model is part of the strategy spec, not a global constant.
4. **Bounded blast radius:** shadow may write `poisoned.flag` into its own run_dir. It may not affect any other process's run_dir, supervisor state, or broker connection.
5. **No live-mode flip:** graduation from shadow to live submission requires a **new run ledger**. Today's `run_ledger.py` already enforces this for free: `submit_mode` enters the hashed `live_config`, so `run_id(shadow_vwap) ≠ run_id(live_vwap)` deterministically. No additional check is required to make Invariant 5 hold.

## Consequences

**Positive:**
- Maximum codepath unity. Every hardening improvement to the live engine (halt detection, indicator-state persistence, SIGINT race, etc.) accrues to shadow for free.
- Graduation to live submission is a strategy-spec edit and a fresh `init-ledger`, not a code migration.
- Divergence Layer A (execution quality) is meaningful only for executing strategies; Layer B (canonical replay) is meaningful for both. The clean factoring falls out of `execution_source` being a column, not a code branch.
- Process isolation between strategies preserves the existing single-strategy-per-process invariant of `LiveEngine` (the `len(ctx.symbols) == 1` guard does not need to relax).

**Negative:**
- `host_daemon.py`'s current `_current: ManagedProcess | None` must grow to a `_managed: dict[strategy_instance_id, ManagedProcess]` registry. Required net-new refactor (PR-A in the deployment plan's § 16 PR queue).
- Strategy specs must declare a `clientId` namespace so the executing and shadow processes do not collide at the Gateway. Schema growth in `StrategySpec` (PR-B in the queue).
- The indicator-state sidecar pattern from PR #239 must be parameterized over `strategy_instance_id` (currently scoped to `spy_ema_crossover/SPY_15m`). Small mechanical extension; the path becomes `artifacts/live_state/<strategy_instance_id>/<bar_descriptor>.json`.

**Non-consequences (decisions this does not force):**
- Whether shadow is on by default for a freshly ported strategy: yes, by convention. Live submission requires the explicit graduation step (new ledger, `submit_mode = "live_paper"`).
- Whether two executing strategies can run concurrently: out of scope. The shadow pattern does not unblock concurrent executing strategies; that requires the deferred broker-executor / virtual-book separation (Step 6 in the revised roadmap).
- Whether shadow can run without an IBKR connection at all: no. Data-source isolation requires shadow to consume the same IBKR bar stream the executing strategy sees. Polygon canonical comparison is a post-hoc replay layer (Layer B), not the shadow's live feed.

## References

- `PythonDataService/app/engine/live/live_engine.py` — engine to be made adapter-polymorphic at the order boundary.
- `PythonDataService/app/broker/ibkr/orders.py` — `IbkrBrokerAdapter`'s current submission path; `NoSubmitBrokerAdapter` is its sibling.
- `PythonDataService/app/engine/strategy/spec/fixtures/spy_ema_crossover.spec.json` — strategy-spec growth template.
- `docs/archive/plans/ibkr-paper-deployment-plan.md` § 16 — historical design-lock resolutions and PR queue.
