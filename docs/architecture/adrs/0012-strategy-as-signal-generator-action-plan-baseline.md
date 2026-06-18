# ADR 0012 — Strategy-as-signal-generator / action plan as deploy-time instrument declaration (baseline)

**Status:** Proposed 2026-06-18. Slice 1A (#594) lands the foundational plumbing only — empty-plan schema, deploy-boundary acceptance, ledger hashing, registry capability flag, `/status` surfacing, cockpit read-only card. Slices 1B (#595), 1C (#596), 1D (#597), 1E (#598) extend the same surfaces with stock/option legs, parity diagnostics, and redeploy lineage. Engine consumption is **out of scope** for the PRD; it lands in Slice 4 alongside a follow-up ADR 0013.

**Decision drivers:** Strategy code today decides *both* when to act and what to act on — signal logic (EMAs crossed, RSI 50–70) and instrument logic (buy 1 SPY at market) are interleaved in one Python file. `process_bar` calls `set_holdings` / `liquidate` directly. There is no clean seam to reuse a SPY-EMA-crossover signal against a stock one day and an iron condor the next without forking the strategy. The deploy/cockpit is the natural place to **declare** which instruments the bot opens on entry and closes on exit; the strategy stays the signal generator. ADR 0009 (live sizing authority) established the parallel pattern for *quantity*; this ADR establishes the same seam for *instrument*.

**Related:** ADR 0009 (live sizing authority — quantity stays here; this ADR does NOT supersede 0009), ADR 0006 (deploy / `account_id` / `live_config` hashed into `run_id` — `live_config.action` joins the existing hash inputs), ADR 0004 (instance-addressed operator control plane — the cockpit card surfaces from `/status`), PRD #593 (this slice's parent PRD; Slices 1–3 only), follow-up ADR 0013 (deferred, Slice 4 PRD: `SignalIntent`, `StructureSizer`, the one-to-many `Decision → ActionIntent → Execution` artifact model, runtime guard against direct portfolio calls).

## Context

Today's surfaces (verified against repo state at this commit):

| Concern | Today |
|---|---|
| Strategy → broker seam | Strategies call `live_portfolio.set_holdings(symbol, fraction)` or `liquidate(symbol)` inside `process_bar`. The instrument and the broker action are baked into strategy code. |
| `live_config` ledger keys | `LIVE_CONFIG_LEDGER_KEYS` (in `app/engine/live/config.py`) is the master allow-list; the deploy boundary rejects unknown siblings (`HostRunnerDeployRequest._validate_sizing`). Pre-Slice-1A: `{symbol, force_flat_at, consolidator_period_min, run_dir, max_submit_latency_ms, sizing}`. |
| `run_id` derivation | `compute_run_id` (in `app/engine/live/run_ledger.py`) hashes the full `live_config` dict. Two deploys with identical hashed inputs collapse to the same `run_id` (idempotent-redeploy contract). |
| Strategy registry capability flags | `app/routers/engine.py::StrategyRegistration` already exposes `sizing_surface ∈ {policy, explicit}` (ADR 0009 §6). The cockpit's Sizing card reads it; the runtime portfolio (`live_portfolio.set_holdings`) enforces it. |
| Quantity authority | `live_config.sizing` per ADR 0009 — `OrderSizer` is the sole quantity authority. Three policies: `FixedShares`, `FixedNotional`, `SetHoldings`. |

The gap: there is no schema, no ledger field, and no UI surface for the operator to declare which **instrument(s)** the bot trades. The instrument is whatever the strategy code names. Reuse across instruments requires a code edit + redeploy.

## Decision

### 1. Two-stage separation: signal generation vs. instrument declaration

Strategy code emits signals; the deploy-form's **action plan** declares which instruments the bot opens on entry and closes on exit. The two are content-hashed independently into `run_id`:

* `live_config.sizing` (ADR 0009) — quantity authority. Unchanged.
* `live_config.action` (this ADR) — instrument authority. **New.**

The action plan is a structured Pydantic v2 model — `ActionPlan{on_enter: ActionEntity[], on_exit: ExitEntity[]}`. Slice 1A ships only the empty-plan envelope so the schema, deploy boundary, ledger, hash, `/status`, and cockpit have a stable container to round-trip through before the leg shapes land in #595 / #596.

### 2. Schema seam — `live_config.action`

* New Pydantic v2 model `ActionPlan` at `app/schemas/action_plan.py`. Slice 1A: `{on_enter: [], on_exit: []}`. Slices 1B/1C extend `ActionEntity` / `ExitEntity` with stock and option leg variants and selector discriminated unions.
* `LIVE_CONFIG_LEDGER_KEYS` extends with `"action"`. The deploy validator (`HostRunnerDeployRequest._validate_sizing`) delegates `live_config.action` to `ActionPlan.model_validate` so malformed plans are rejected at the boundary, not after `run_id` is computed.
* `run_id` includes `live_config.action` in its hash (it's just one more key in the existing dict-hash). Identical action plans deployed twice collapse to the same `run_id`; a different plan produces a different `run_id`.

### 3. `leg_id` lifecycle (forward-looking — applies once #595 lands legs)

Every entry leg carries a stable `leg_id` (operator-named or auto-assigned, regex `^[a-z0-9_]{1,32}$`). Exit entries are lifecycle actions that reference `entry_leg_id`s — `close_leg` in Slice 1, future variants (`roll`, etc.) deferred. Exit entries **never** redeclare strike or expiry selectors; re-resolving `atm` or `min_dte` at exit time would close the wrong contract once consumption lands. The `leg_id → resolved IBKR conId` map is the resolver's responsibility (Slice 4); this ADR fixes the identity contract so the resolver has stable keys to persist against.

### 4. `qty_ratio` is purely declarative in Slices 1–3

Each entry leg carries a positive-integer `qty_ratio`. **Composition semantics (how `qty_ratio` × `live_config.sizing` becomes a fill quantity) is deferred to Slice 4** because the three ADR-0009 sizing kinds (`FixedShares`, `FixedNotional`, `SetHoldings`) need different inputs (structure price, option multipliers, portfolio value, margin) before "structure units" exist. The Slice 4 PRD will land a follow-up `StructureSizer` ADR. **ADR 0009 is NOT superseded** — `OrderSizer` remains the sole quantity authority.

### 5. Explicit `instrument.underlying` — no implicit fallback

Every leg carries an explicit `instrument.underlying` (e.g. `"SPY"`). The deploy form **may** prefill from `live_config.symbol` as UX sugar, but the **stored plan always carries the literal value**. Rationale: the plan is self-contained, the preview endpoint (#597) is context-free, and an action plan trading `QQQ` on a strategy that watches `SPY` bars is expressible without surprise canonicalization.

### 6. `instrument_surface ∈ {policy, explicit}` capability flag — informational in Slices 1–3

New `StrategyRegistration.instrument_surface` field — parallels `sizing_surface` per ADR 0009 §6. **Every current strategy registers as `"explicit"`** in Slices 1–3 (set as the default so adding a strategy doesn't silently adopt `policy`). The deploy boundary **stores** the registered value in the run ledger (via `/status`) but does **NOT refuse** deploys based on it — informational only. Slice 4 introduces enforcement: a `"policy"`-surface strategy that calls `set_holdings` directly will be rejected at runtime, mirroring the `sizing_surface="policy"` ↔ `set_holdings` enforcement in `live_portfolio.py`.

### 7. Unhashed redeploy lineage — `parent_run_id` + `redeploy_reason` (Slice 1E #598)

`HostRunnerDeployRequest` accepts optional top-level `parent_run_id` and `redeploy_reason` fields. They are persisted in the ledger's `lineage` block alongside other unhashed metadata (`code_sha`, `sizing_provenance`, `created_at_ms`). They are **deliberately NOT in `LIVE_CONFIG_LEDGER_KEYS`** and **NOT hashed into `run_id`** — otherwise the same plan redeployed from two parents would mint two `run_id`s and break the idempotent-redeploy contract. This is the load-bearing invariant Slice 1E pins.

### 8. Parity diagnostics are warnings, NOT hard errors (Slice 1D #597)

Pydantic enforces hard schema errors (unknown selector, `qty_ratio < 1`, exit references missing `leg_id`, duplicate `leg_id`s, missing `instrument.underlying`) at deploy. **Parity** mismatches (orphan entry leg with no matching `close_leg`, asymmetric position direction) are computed by a pure function `parity_diagnostics(plan) → list[ParityWarning]` exposed via `POST /api/live-instances/preview-action-plan`. Warnings are non-blocking — the operator can submit a plan with warnings because asymmetric structures are legitimate (calendar rolls, leg-by-leg unwinds).

### 9. Cockpit honesty — "not active until engine consumption (Slice 4)"

The `<app-action-plan-card>` carries the explicit literal label *"Declared action plan — not active until engine consumption (Slice 4)"*. A run's `run_id` captures **declared intent**, not what was traded. Post-hoc reconstruction can read the ledger to recover the operator's declaration; it cannot infer that the bot acted on it during Slices 1–3 because the engine does not consume `live_config.action` until Slice 4.

## Consequences

### Positive

* The strategy → instrument seam exists. Slice 4 can wire engine consumption without a schema migration; everything `run_id`-load-bearing is already in place.
* `run_id` honestly attests to declared intent — adding `action` to the hash makes the content-addressed identity strictly more specific. Pre-Slice-1A and Slice-1A runs hash differently even when every other input is identical (a test pins this).
* The cockpit gains an honest read-only surface for the declared plan. The label removes any ambiguity that the bot is acting on the declaration in Slices 1–3.
* `instrument_surface` provides a forward-compatible enforcement seam (Slice 4) without changing the deploy boundary today.
* Idempotent-redeploy contract is preserved (Slice 1E lineage fields are unhashed by design).

### Negative / costs

* Adds a new `live_config` ledger key. The deploy boundary now delegates to a second Pydantic model (`ActionPlan`), increasing validator surface. Mitigated by reusing the existing `_validate_sizing` precedent — same `unknown sibling keys` allow-list pattern, same delegated round-trip via `policy_to_ledger_dict` / `ActionPlan.model_validate`.
* `run_id` hashing now depends on a second nested structure. Future-self risk: if the schema's field ordering or default serialization changes between Pydantic versions, `run_id` could drift silently. Mitigation: explicit round-trip via `ActionPlan.model_validate(...).model_dump()` so the canonical dict shape is what enters the hash, not the operator's raw input.
* The cockpit card and the deploy-form picker are duplicated work across Slices 1B and 1C (one extends them with stock, the next with options). Accepted because the schema variants are different enough that one big-bang slice would be unreviewable.

### What this ADR does NOT decide (deferred to ADR 0013 / Slice 4)

* The engine-consumed contract between signal generation and instrument resolution. Provisionally `SignalIntent(signal, bar_close_ms, intended_price, metadata)` — the real shape is Slice 4's call.
* `StructureSizer` — how `qty_ratio` × `live_config.sizing` becomes a fill quantity across the three ADR-0009 sizing kinds.
* The runtime guard against `instrument_surface="policy"` strategies calling portfolio order methods directly. Parallels the existing `sizing_surface` enforcement in `live_portfolio.py`.
* The one-to-many `Decision → ActionIntent → Execution` artifact model. `ExecutionRow` exists today (`app/engine/live/artifacts.py`) and is **untouched** in Slices 1–3. `ActionIntent` ships only when its consumer ships.
* Delta-target strike selector — requires chain lookup + per-strike delta computation. Slice 6 (`delta` is deliberately absent from the Slice-1C deployable schema until then so operators cannot deploy a plan the engine cannot run).

## Anti-patterns this ADR rejects

* Hashing `parent_run_id` / `redeploy_reason` into `run_id` — would break idempotent-redeploy.
* Implicit fallback from `live_config.symbol` to `instrument.underlying` — leaves the plan context-dependent and the preview endpoint stateful.
* Re-declaring strike / expiry selectors at exit time — re-resolving `atm` or `min_dte` would close a different contract than was opened.
* Treating parity warnings as hard errors — asymmetric structures are legitimate.
* Shipping speculative `ActionIntent` / `Execution` Pydantic types in Slice 1 — they would drift before Slice 4 lands the real resolver/broker contracts. `ExecutionRow` already exists.
* Loosening the `instrument_surface` capability flag to refuse deploys in Slices 1–3 — no current strategy can be refused (every one is `explicit`), and the deploy form's picker is shown unconditionally so operators can declare plans before consumption lands.
