# ADR 0009 — Live position sizing is a `live_config` policy resolved in Python, with engine-derived provenance; the strategy spec is **not** the live sizing authority

**Status:** Proposed 2026-06-08 — re-opened 2026-06-13 for a **UI re-think** after the broker control panel cockpit grew significantly (provenance card, instance console, paper-run, candlestick + trades, run-log "Fix this"). **Substance** — engine plumbing, allow-list mechanism, provenance stamps, four kinds, `FixedShares(1)` default, fail-closed Reference parity — **is unchanged**. The deltas are UI placement, the per-trade audit mechanism, the allow-list file shape, the coexistence guard's scope, and PR sequencing — captured in the **Updated 2026-06-13 — UI re-think** section below. Vocabulary recorded in `CONTEXT.md` § "Sizing authority". Original grilling: `grill-with-docs` 2026-06-08; re-grill: `grill-with-docs` 2026-06-13. Load-bearing code claims adversarially verified before each session.
**Decision drivers:** The first live deployment-validation run sized with `set_holdings(SPY, 1.0)` (target **fraction 1.0 = 100% of equity**) and bought ~336 shares (~$250k) — the entire paper account — on one signal (`docs/position-sizing-research-handoff.md`). Nine of the equity strategies target 100% of equity; two are self-sized; **none can coexist on one account**. Sizing is fused into each strategy's signal code, so it cannot change without editing (and re-reconciling) the algorithm. A grilling session designed the fix and surfaced two traps a naïve version would hit: (1) the **live runtime executes hand-coded algorithms, not the strategy spec**, so "make sizing a declarative spec field" alone changes nothing live; (2) a declarative `SizeRule` **already exists** in the spec (`SetHoldings | FixedContracts`), so a parallel sizing vocabulary would violate the single-source-of-truth rule (`CLAUDE.md` guiding principle #5).
**Related:** ADR 0006 (deploy / content-addressed `run_id` / `live_config` hashing — **directly extended**), ADR 0001 (JSON/Parquet substrate), ADR 0008 (durable submit & order identity), `CONTEXT.md` § "Sizing authority", `.claude/rules/numerical-rigor.md` ("numerical claims require receipts"), `docs/position-sizing-research-handoff.md` (this ADR resolves its open briefs and **supersedes its § 3**).

## Context

**Everything named below as a new component is net-new — none of it exists today.** This ADR creates it. The current state (verified against the code):

| Concern | Today |
|---|---|
| What runs live | `run.py` does `import_module(f"app.engine.strategy.algorithms.{strategy}")` and executes a **hand-coded algorithm class**. `SpecAlgorithm` / the declarative spec runs only in backtest/research/parity — **never live**. |
| How strategies size | 7 equity algos call `ctx.set_holdings(symbol, Decimal(1))` (target **fraction** 1.0); `spy_vwap_reversion` calls `ctx.market_order(symbol, 100)`; `spy_ema_crossover_options` sizes internal `contracts_per_trade` and never touches the equity portfolio. |
| Live sizing model | `LivePortfolio.sizing_model` defaults to `SimpleFloorSizing` (no buffer, ignores fees) and is **never overridden** — `live_engine.py:398` constructs `LivePortfolio(self._broker)`, and `LiveEngine.__init__` has **no `sizing_model` parameter**. `LeanSetHoldingsSizing` is wired **only** in `cross_runner.py` (LEAN parity backtests). |
| Declarative sizing that *does* exist | `spec/schema.py:290` — `SizeRule = SetHoldings{fraction 0<f≤1} | FixedContracts{value≥1}`, read by the spec evaluator (`evaluator.py:389`). `FixedContracts` is reserved for options and `raise`s `NotImplementedError` for equity. Spec-level, backtest-only. |
| `live_config` identity | Hashed into the content-addressed `run_id` (`run_ledger.py:124-133`, 7 fields). The hasher is nested-dict-stable, so a nested `sizing` block hashes deterministically **for free**. |
| `live_config` validation | `_live_config_from_ledger` (`run.py:540-549`) hard-whitelists 5 keys and **`raise`s `ValueError` on any unknown key**, failing the start (exit 2). `HostRunnerDeployRequest.live_config` (`schemas/live_runs.py:308`) is an **untyped `dict`**. Adding a `sizing` key is rejected until this gate is extended — this is the load-bearing code change. |

The handoff doc (§3) assumed switching `deployment_validation` to 1 share "requires re-running the QuantConnect parity backtest … because changing sizing changes the algorithm." That was true *in the sizing-fused-into-the-algorithm world*. This ADR moves sizing **out** of the algorithm, which makes the assumption false — see Decision 8.

## Decision

### 1. Canonical live sizing is `run_ledger.live_config.sizing` — not the spec

The **canonical** sizing authority for a *live* bot is `live_config.sizing`. The spec's `SizeRule` and a hand-coded algorithm's `set_holdings(…, 1.0)` are **reference/default metadata**, not the live authority — because the live runtime does not execute the spec. Making `spec.entry.size` canonical-for-live while hand-coded algorithms actually run would be a false source of truth ("hashed but not executed"). `spec.entry.size` becomes canonical **only** for a bot whose live runtime executes `SpecAlgorithm` — a deliberate future state, not this ADR.

Because `live_config` is already hashed into `run_id` (ADR 0006), sizing becomes **audited deployment identity** for free: any sizing change mints a new `run_id`. The launch page is the operator boundary where this account-risk decision belongs; **Angular only selects the policy, Python resolves the quantity** — Python stays the math authority.

### 2. The live `live_config.sizing` union — four kinds, typed, validated at both boundaries

```
SetHoldings    { kind: "SetHoldings",    fraction: float (0,1] }
FixedShares    { kind: "FixedShares",    value: int ≥ 1 }
FixedNotional  { kind: "FixedNotional",  value: str (decimal — money, never float) }
StrategyExplicit { kind: "StrategyExplicit" }
```

A Pydantic **discriminated union** (on `kind`), validated at **both** untyped boundaries the code has today: `HostRunnerDeployRequest.live_config` (deploy API) and `_live_config_from_ledger` (run start). It rejects unknown sizing keys, float money values, non-positive quantities, and **combinations disallowed by `sizing_surface`** (Decision 6). The spec's `FixedContracts` stays spec-only (options) and is untouched. The broader `live_config` allow-list/`LiveConfig`-dataclass drift (the allow-list already lags the dataclass) is **out of scope** — extend the allow-list narrowly for `sizing`; leave the full `LiveConfig`-Pydantic rewrite as a separate cleanup (compatibility-preserving).

### 3. Two engine-derived, un-forgeable provenance stamps

Sizing carries two **engine-derived** ledger stamps, never operator-supplied (provenance is a verified fact, not an assertion — same spirit as ADR 0008's "provenance is not identity"):

- **`governed_by`** ∈ `{live_config, strategy_explicit}` — *who* set the quantity (the deploy-page policy via `set_holdings`, vs the strategy's own `market_order`/`contracts`).
- **`sizing_provenance`** ∈ `{reference_native, live_override, spec_default(reserved)}` — *does the resolved sizing match the bound QC audit copy.* `reference_native` requires **rule-level** equivalence (same sizing rule as the QC algorithm — e.g. both `SetHoldings(1.0)`), **not** a coincidental share count. `spec_default` is reserved until the live runtime executes `SpecAlgorithm`; not emitted today.

The proof backing `reference_native` is an **audit-copy sizing allow-list**: a narrow map `qc_audit_copy_sha256 → known sizing rule`, **not** AST-parsing of arbitrary LEAN code ("numerical claims require receipts"). It has three outcomes — **proven match / proven mismatch / cannot prove** (sha absent). The default is **fail-closed**: anything other than proven-match stamps `live_override`. The **Reference parity** preset (Decision 7) proceeds **only on proven match**; proven-mismatch *and* cannot-prove both **block** the deploy — never a silent downgrade to `live_override` (the preset's name is a promise; breaking it silently is the audit-UX failure this design exists to prevent). The two stamps are orthogonal: a `strategy_explicit` run can still be `reference_native` (e.g. `spy_vwap_reversion`'s explicit 100 matches a QC copy that also trades 100).

### 4. Interception contract — the policy governs `set_holdings` only

The policy is applied at the `LivePortfolio` boundary and governs **`set_holdings` only**. `set_holdings(symbol, fraction)` is a *target-position intent* (direction + go-to-target); the policy **reinterprets the magnitude** per kind:

- `SetHoldings(f)` → the fraction path (Decision 5).
- `FixedShares(n)` → target `n` shares (`fraction > 0` → `n`; `fraction == 0` → flat; **long-only in v1**, no accidental short).
- `FixedNotional(v)` → `floor(Decimal(v) / reference_price)` shares.
- `StrategyExplicit` → no policy sizing.

`market_order(symbol, qty)` and options `contracts_per_trade` are **explicit strategy sizing and are never overridden** (TradingView doctrine: explicit quantity wins). `liquidate(symbol)` is **always target-flat, never size-policy modified** — it is a flatten command, not a sizing surface. A policy that resolves to a **zero** share target while flat does **not** submit a zero order; it logs a **sizing skip** so the operator sees why no entry fired (applies to every kind). A blanket quantity cap is **not** position sizing — if ever needed it is a separately-named **risk overlay** (Decision 9), not this policy.

### 5. The resolver — `order_sizer.py`, a thin adapter above the single quantity-math authority

A new `engine/execution/order_sizer.py` is the **canonical live policy-application layer**. It is an **adapter**, not a parallel hierarchy: it delegates the percent path to the existing **`engine/execution/sizing.py::LeanSetHoldingsSizing`**, which remains the **canonical `SetHoldings` quantity-math authority** (it is golden-fixture-pinned at `atol=0`). `FixedShares`/`FixedNotional` bypass `SizingModel` (they need no `target_fraction`); `StrategyExplicit` is a no-op.

The percent path resolves through `LeanSetHoldingsSizing(fee_model=IbkrEquityCommissionModel())` — buffered, fee-aware, **what QC's `SetHoldings` actually does**. This **retires `SimpleFloorSizing` from the live path** (it stays a research/backtest model). That is a **deliberate live behavior change**: every existing `SetHoldings` live deploy will compute a different (smaller, buffer/fee-reserved) share count. It ships with a **pinned regression test** documenting the intentional shift and a `docs/references/` note, per numerical-rigor.

The resolver is **not** placed in `engine/framework/` for v1. `framework/` is an explicitly partial LEAN Algorithm Framework port (Insight → AlphaModel → … → PortfolioConstruction → Execution); homing the live resolver there now would imply a second portfolio-construction authority before the live runtime actually runs through that framework. When the live runtime becomes spec/framework-driven, the resolver migrates into `framework/` as the `PortfolioConstructionModel` — a future move.

### 6. `sizing_surface` registration attribute + `StrategyExplicit` + order-surface fail-fast

`StrategyRegistration` (`routers/engine.py:341`) gains a declarative **`sizing_surface: "policy" | "explicit"`** (named for the boundary, not a bare `self_sized` bool — leaves room for a future `mixed` / `portfolio_model`), defaulting to `"policy"`. `spy_vwap_reversion` and `spy_ema_crossover_options` are `"explicit"`.

- `policy` surface → strategy targets via `set_holdings`; `live_config.sizing` ∈ `{FixedShares, FixedNotional, SetHoldings}` governs; deploy-form sizing control **enabled**.
- `explicit` surface → strategy supplies its own quantity/contracts; the **required** `live_config.sizing` is `StrategyExplicit` (an **honest** ledger value, never a misleading `FixedShares(1)`); deploy-form sizing control **disabled + labeled "self-sized."**

`governed_by` derives from the kind (`StrategyExplicit → strategy_explicit`, else `live_config`) and is **runtime-validated** against the actual order surface used (`set_holdings | market_order | liquidate | internal_strategy_accounting`). A mismatch on an **entry** order is a registration bug → **fail-fast on the first mismatched entry order**, never continue with a misleading ledger. `liquidate()` is never a violation.

### 7. Presets, the global default, and always-present sizing

The launch page offers **named presets** that fill `live_config.sizing`:

- **Safe canary** — `FixedShares(1)`. The **global default for every new live deploy.** All-in is now **explicit opt-in**.
- **Reference parity** — `SetHoldings(1.0)`. **Gated**: usable only when the audit-copy allow-list proves the bound QC rule is `SetHoldings(1.0)` (proven-match); otherwise blocked (Decision 3). Routes through `LeanSetHoldingsSizing`.
- **Custom** — `FixedShares(n)` / `FixedNotional(v)`. No reference promise; always `live_override`.
- **Deferred** — arbitrary fractional `SetHoldings(f)` for `0 < f < 1` (no live strategy needs a partial fraction today).

Every new deploy **always writes an explicit `sizing` block.** **Absence** of `sizing` means **legacy/unknown** (pre-policy `SimpleFloorSizing` all-in), *never* `FixedShares(1)` — so old empty-`live_config` runs never hash-collide with the new safe default. The resulting `run_id` churn vs the empty-`live_config` era is a non-issue: live deployment is days old (first clean session 2026-06-08), and all-in runs *should* carry different identities than fixed-1 runs.

### 8. The canary fix is config-only — this supersedes the handoff doc's QC-re-cut claim

With this design, switching `deployment_validation` to 1 share is a **pure `live_config.sizing = FixedShares(1)` deploy**: **no `.py` edit, no spec edit, no QC re-cut.** Reasoning (the supersession): the algorithm still calls `set_holdings(symbol, Decimal(1))` unchanged, so `code_sha`, the QC audit copy, `qc_audit_copy_sha256`, and `qc_cloud_backtest_id` are all unchanged; only `live_config` changes, so `run_id` changes but the QC **reconciliation anchor stays valid as a *signal* anchor**. The policy reinterprets the magnitude (fraction 1.0 → 1 share) at the interception boundary. The run is honestly stamped `governed_by = live_config`, `sizing_provenance = live_override`, and the ledger/reconciliation report reads *"signal logic anchored to QC, sizing overridden by live config."*

This is only true **after** this ADR is implemented. Today, with no `live_config.sizing` field and the allow-list rejecting it, the 1-share switch *would* be an algorithm edit — which is exactly the world `docs/position-sizing-research-handoff.md` § 3 (lines 148–152) describes. **This ADR supersedes that paragraph**: sizing is decoupled from the algorithm, so the canary fix no longer requires re-cutting the QC anchor.

### 9. Deferred — with the seams named

- **Cross-strategy capital allocation (Brief B) — deferred.** The `FixedShares(1)` default *dissolves* the original coexistence failure for the common case (N bots at 1 share each contend no buying power). The collision only returns for two `SetHoldings` (all-in) bots. The future **capital sleeve** layer sits at the **portfolio-value provider** feeding `order_sizer`'s `SetHoldings` path (whole account today → per-strategy sleeve later → `LeanSetHoldingsSizing`); `FixedShares`/`FixedNotional` never read it. A **pass-through provider is introduced in v1** so the sleeve is a later drop-in. The interim stand-in is an **all-in coexistence guard**: a start/pre-flight refusal when resolved sizing is `SetHoldings(1.0)` *and* the account is non-flat or another managed all-in bot is active.
- **`capital sleeve` ≠ `allocation`.** The Python live buying-power budget is a **capital sleeve**; the `.NET`/Postgres `StrategyAllocation.CapitalAllocated` is an after-the-fact attribution record. The two words must not be conflated across stacks.
- **Risk overlays (Brief E) and advanced sizing (Brief A: risk-per-trade, ATR, vol-targeting, fractional-Kelly) — deferred** until strategies declare the required inputs (stop distance, ATR/vol window, expected edge, max-risk caps). They are policies, not the right default for a canary.
- **"Don't let the launch page claim to override a pasted algorithm's hardcoded sizing" guardrail — already honored**: live runs only vetted, named modules; a self-sized algorithm is `sizing_surface=explicit`, which disables the launch sizing control. No v1 code needed.

## Consequences

**Positive:**
- The all-in foot-gun becomes **opt-in**: every deploy defaults to 1 share, and all-in is reachable only via a gated, receipt-backed preset. The $250k surprise cannot recur silently.
- Sizing becomes **audited deployment identity** (hashed into `run_id`) with **un-forgeable** provenance — `reference_native` is a verified comparison, not an operator string.
- Single source of truth preserved: **one** quantity-math authority (`LeanSetHoldingsSizing`), with `order_sizer.py` as a thin policy adapter above it. No parallel sizing vocabulary.
- The **canary fix is config-only** — no algorithm edit, no QC re-cut — which unblocks deterministic, cheap live-plumbing validation (1 share, single fill).
- Multi-strategy coexistence is **solved for the default case** without building the allocation layer.

**Negative:**
- Net-new plumbing before any form exists: `order_sizer.py`, the Pydantic sizing union, the allow-list extension, threading a resolved policy `live_config → LiveEngine → LivePortfolio` (none of which exists today — `LiveEngine` has no `sizing_model` param), the audit-copy allow-list, and the `sizing_surface` attribute.
- Retiring `SimpleFloorSizing` from the live path **changes live share counts** for every `SetHoldings` strategy — intentional, but it requires a pinned regression and a provenance note.
- Surfacing `sizing_surface` to the **deploy form** requires widening the **trimmed `EngineStrategyInfo`** type (`live-runs.types.ts:272` / `live-runs.service.ts:214`) — the deploy form does *not* read the full `StrategyInfo` (the Engine-Lab `lean-engine` picker does, and gets it for free).
- The Reference parity preset is **unavailable for an audit copy whose sha is not in the allow-list** until its sha + rule are registered — a deliberate fail-closed cost.

**Non-consequences:**
- The on-disk substrate is unchanged (ADR 0001); the spec's `FixedContracts` (options) is untouched.
- This ADR does **not** build the capital-sleeve / cross-strategy allocation layer, risk overlays, or any volatility/Kelly sizing — only the policy seam and the safe default.
- The spec → live execution migration is **not** decided here; `spec.entry.size` remaining non-canonical-for-live is explicit, not an oversight.
- ADR 0006's deploy pipeline and QC-anchor-mandatory rule are unchanged; this ADR adds a `live_config` field within that pipeline.

## References

- `PythonDataService/app/engine/live/run_ledger.py:124-133` — `compute_run_id` (the 7 hashed fields; `live_config` independent of the QC-anchor fields).
- `PythonDataService/app/engine/live/run.py:540-549` — `_live_config_from_ledger` allow-list that `raise`s on unknown keys (the load-bearing gate); `:854-861` start-failure path.
- `PythonDataService/app/engine/live/config.py:16-47` — `LiveConfig` dataclass (no `sizing` field today).
- `PythonDataService/app/engine/live/live_engine.py:398` — `LivePortfolio(self._broker)` (no sizing injection); `:240-284` — `LiveEngine.__init__` has no `sizing_model` param.
- `PythonDataService/app/engine/live/live_portfolio.py:211` — `sizing_model: SizingModel = field(default_factory=SimpleFloorSizing)`; `:268-291` — `set_holdings` → `target_quantity` → delta market order.
- `PythonDataService/app/engine/execution/sizing.py:46-61` — `SizingModel` protocol; `:88-137` — `LeanSetHoldingsSizing` (the quantity-math authority the resolver delegates to).
- `PythonDataService/app/engine/execution/order_sizer.py` — **new**, the live policy-application adapter.
- `PythonDataService/app/engine/strategy/spec/schema.py:290-302` — existing `SizeRule` (`SetHoldings | FixedContracts`), spec-level, backtest-only.
- `PythonDataService/app/engine/strategy/algorithms/deployment_validation.py:157` — `set_holdings(symbol, Decimal(1))` (fraction 1.0); `spy_vwap_reversion.py:117` — `market_order(symbol, 100)` (explicit surface).
- `PythonDataService/app/routers/engine.py:340-363` — `StrategyRegistration` (gains `sizing_surface`); `:1583,1617-1627` — `StrategyInfo` construction (one line surfaces it).
- `Frontend/src/app/api/live-runs.types.ts:272` / `Frontend/src/app/services/live-runs.service.ts:214` — trimmed `EngineStrategyInfo` the deploy form reads (must be widened for `sizing_surface`).
- `CONTEXT.md` § "Sizing authority" — the operator vocabulary this ADR implements.
- `docs/position-sizing-research-handoff.md` — research briefs A–E; § 3 superseded by Decision 8.

## Updated 2026-06-13 — UI re-think

Re-grilled in a `grill-with-docs` session after the broker control panel grew (provenance card, instance console with `bot-trades-table` / `bot-failures-table` / `bot-trade-chart-card`, paper-run page, run-log "Fix this" modal, freshness verdict, last-exit halt-trigger, options surface). **None of that changed any sizing assumption** — verified by code reading: `live_config.sizing` still does not exist, `_live_config_from_ledger` still rejects unknown keys at `run.py:540`, `LivePortfolio.sizing_model` still defaults to `SimpleFloorSizing` and is never overridden, `LeanSetHoldingsSizing` is still wired only in `cross_runner.py`. The ADR's engine work is exactly as load-bearing as it was on 2026-06-08. The re-think is about *where* the operator sees this and *what order* to land it.

### 10. UI placement — dedicated Sizing card, deploy-form preset selector, provenance card stays scoped

The cockpit has three potential surfaces for the sizing concept: the deploy form (pre-deploy choice), the provenance card (run-identity fingerprints), and the instance console (operator's day-to-day view). We picked **three slots, each with a tight scope**:

- **Deploy form** — a **3-option radio + inline Custom expansion** (Safe canary / Reference parity / Custom) inserted between the QC binding section and the "start trading immediately" toggle. The Reference parity option carries an **inline gate status** rendered server-side from the allow-list lookup for the bound `qc_audit_copy_path` ("audit copy proves SetHoldings(1.0)" / "audit copy not in allow-list" / "audit copy is `SetHoldings(0.5)`, not the v1-supported `SetHoldings(1.0)`"). Selecting Custom expands a kind dropdown (`FixedShares` | `FixedNotional`) + value input. For `sizing_surface=explicit` registrations, the whole control is disabled and labelled "self-sized" (Decision 6, unchanged).
- **Provenance card** stays **scoped to run-identity fingerprints** (`run_id`, `code_sha`, `strategy_spec_sha256`, `qc_audit_copy_sha256`, `qc_cloud_backtest_id`). It is **not** the home for sizing detail. Adding sizing would conflate identity-fingerprint provenance with sizing provenance; they are orthogonal stamps that deserve orthogonal surfaces.
- **Sizing card** — a **new dedicated card** on the instance console, next to the provenance card, with three sections:
  1. **Static facts** (run-bound): preset name, `live_config.sizing.{kind, value}`, `governed_by`, `sizing_provenance`, and the **audit-copy verdict** with the diff spelled out for *proven mismatch* and *cannot prove* outcomes ("audit copy rule is `SetHoldings(1.0)`; your live sizing is `FixedShares(1)`" — sized-live derivative narrative).
  2. **Live derivation** (refreshed at the latest price): resolved share count this policy would produce *right now* (for `SetHoldings` / `FixedNotional`; for `FixedShares` it's static), and the **sizing-skip** counter for the session.
  3. **Per-trade audit list** (Decision 11).
- **Instance binding** — the card mounts only when a live binding exists for the instance; for completed runs or dead instances it shows from the latest evidence-binding run, clearly labelled. Stale evidence is never a control surface (CONTEXT § "Binding authority", unchanged).

### 11. Per-trade audit — a new `SIZING_RESOLVED` WAL event

The per-trade audit list joins each broker fill to the sizing decision that produced its order. This requires persisting the sizer's resolved `(policy_kind, intended_qty, reference_price, sizing_provenance_at_resolve_time)` tuple alongside the eventual fill so the join is on `intent_id`. The canonical home is the WAL.

- A new **`SIZING_RESOLVED`** event in `intent_events.jsonl`, appended **before** `SUBMITTED` / `ACK_FAILED_UNCERTAIN`, carrying `{ts_ms_utc, intent_id, policy_kind, policy_value, intended_qty, reference_price (str/decimal), sizing_provenance_at_resolve_time, sized_via}`. The event captures the sizing decision at the moment the order was constructed — not at fill time, not at session boundary.
- The fold extends `submitted_orders[intent_id]` in the projection (`live_state.json`) with a **`sizing_resolution`** field of the same shape. The trades query joins on `intent_id`.
- **`sized_via`** values: `policy_set_holdings` (the policy intercepted `set_holdings`), `policy_market_order` (reserved for future migration), `strategy_explicit_market_order`, `strategy_explicit_contracts`. For v1, only `policy_set_holdings` and the two `strategy_explicit_*` values are emitted. Adding the others does not bump the WAL contract (Decision 6's `sizing_surface` already distinguishes them).
- **No backfill.** Legacy runs (pre-PR1) have no `SIZING_RESOLVED` events; the Sizing card hides the per-trade audit list for them (Decision 13).
- The fail-fast on `order-surface mismatch` (Decision 6) runs against the recorded `sized_via` value, so a `set_holdings`-using strategy registered as `explicit` halts on the first entry order with the misleading-ledger error the ADR already specifies.

### 12. Allow-list location — a single indexed JSON file, sha-verified at load

The audit-copy sizing allow-list lives at **`docs/references/audit-copy-sizing-allow-list.json`** — a single index file:

```jsonc
[
  {
    "audit_copy_path": "references/qc-shadow/DeploymentValidationAlgorithm.py",
    "audit_copy_sha256": "abcd…",
    "rule": { "kind": "SetHoldings", "fraction": "1.0" },  // decimal string; never float
    "registered_at_ms": 1718268000000,
    "registered_by": "inkant"
  }
]
```

The lookup is server-side at the deploy/start boundary. **The entry's `audit_copy_sha256` is re-verified against the on-disk audit copy at load** — a mismatch is *cannot prove*, never a silent override. Adding a new audit copy without an entry is *cannot prove*; adding an entry whose `rule.kind` is not yet wired (e.g. `SetHoldings(0.5)` while v1 only supports `SetHoldings(1.0)`) is *proven mismatch* against the selected preset, so Reference parity remains blocked with the specific reason surfaced in the deploy form's inline gate status. We rejected per-audit-copy sidecar files because the index puts all sizing claims behind one PR-reviewable surface; we rejected a Python module constant because data and code coupling is too tight; we rejected a Postgres table because it adds a runtime dependency for a static lookup.

### 13. Coexistence guard — symbol-scoped, not account-wide

Decision 9's coexistence guard is **narrowed to the trade symbol**:

- Block `SetHoldings(1.0)` start when *either* (a) the bound trade symbol has non-zero exposure in the broker account (any source — managed or not), *or* (b) another managed live binding on this account holds `SetHoldings(1.0)` on the same symbol.
- `FixedShares` / `FixedNotional` are still **never** blocked (unchanged from Decision 9).
- **Permitted-but-unsafe**: two all-in bots on *different* symbols (SPY all-in + AAPL all-in) deploy successfully and *will* fight for shared cash buying power. This is an accepted v1 trade-off, not an oversight — the capital-sleeve layer (still deferred, Decision 9) closes it. The deploy form does not warn on cross-symbol all-in concurrency in v1; the fleet readiness gate may surface it as inherited `DEGRADED` once the fleet-level signal is wired (CONTEXT § "Broker-observed state & position ownership").

The narrower scope was accepted because account-wide blocking would refuse a `SetHoldings(1.0)` SPY-EMA deploy onto an account that holds stray foreign exposure (e.g. an unrelated manual position), which is operationally too strict for the existing baseline run that the QC parity matrix depends on.

### 14. Legacy / pre-policy run rendering

Every existing run today has `live_config = {}` (or 5 keys, none being `sizing`). After PR1 ships, those runs read as **`absence ⇒ legacy/unknown`** (the ADR is already explicit that absence is *not* `FixedShares(1)`). The UI is correspondingly honest:

- The provenance card adds **no sizing row** for legacy runs (the provenance card stays scoped to identity fingerprints anyway).
- The Sizing card renders a **degraded "Pre-policy run" variant**: badge + a one-line explanation ("sized by the historical `SimpleFloorSizing` model; sizing provenance, audit-copy verdict, and per-trade audit are unavailable for pre-policy runs"). Static facts (kind/preset/`governed_by`/`sizing_provenance`/verdict), live derivation, and the per-trade audit list are all suppressed.
- **No ledger backfill** — synthetic `live_config.sizing` would mutate hashed `run_id`s. Absence stays absence.
- **Re-deploy from a legacy run defaults to Safe canary**, not "previously used" — the safe default applies on the first sizing-aware deploy. Operators see and confirm the change inline; the deploy form's deep-link prefill (strategy/spec/account/QC fields) is unchanged.

### 15. Implementation order — tracer-bullet Safe canary first

The original PR sequence (Decision §"v1 PR sequence") was logical but engine-first, which back-loaded operator-facing safety to PR4. The re-think flips to a **tracer-bullet** ordering — Safe canary lands end-to-end first, then later kinds and surfaces layer on. This puts the `$250k surprise can't recur silently` win in the operator's hands as early as possible, and isolates the deliberate `SimpleFloor → Lean` share-count shift to a later PR with the deploy-form UI already explaining it:

1. **PR1 — Safe canary tracer.** `engine/execution/order_sizer.py` with `FixedShares` only; Pydantic discriminated union over `{FixedShares, SetHoldings, FixedNotional, StrategyExplicit}` with `FixedShares` as the only currently-implemented runtime path (others validate but reject with a clear "not yet wired" error if selected); `_live_config_from_ledger` allow-list extension; pass-through portfolio-value provider seam (for the eventual capital sleeve); `LiveEngine` / `LivePortfolio` sizing injection; deploy form's 3-option radio with **Safe canary as the only enabled option** (Reference parity disabled "ships in PR3"; Custom disabled "ships in PR4"); Sizing card static facts (preset / kind / `governed_by` / `sizing_provenance=live_override`); engine-derived stamps in the ledger. **No `SimpleFloor → Lean` cutover yet** — the percent path is not exercised by Safe canary.
2. **PR2 — Lean cutover + regression note.** Wire `LeanSetHoldingsSizing(fee_model=IbkrEquityCommissionModel())` as the percent-path resolver inside `order_sizer.py`; retire `SimpleFloorSizing` from the live path; pinned regression test documenting the intentional share-count shift; `docs/references/lean-set-holdings.md` provenance note. No deploy form change (Reference parity still disabled — the math is wired but the gate isn't).
3. **PR3 — Reference parity end-to-end.** `docs/references/audit-copy-sizing-allow-list.json` with the two existing entries (`DeploymentValidationAlgorithm.py`, `SpyEmaCrossoverAlgorithm.py`); server-side lookup endpoint feeding the deploy form's inline gate status; deploy form enables Reference parity (gate-aware); `sizing_provenance` verdict logic (`reference_native` on *proven match*, `live_override` on *proven mismatch* and *cannot prove*); Sizing card audit-copy verdict + diff line.
4. **PR4 — Custom.** Deploy form Custom expansion (kind dropdown + value input); engine wires the remaining `FixedShares(n>1)` and `FixedNotional` paths; Sizing card live derivation (resolved-shares-at-latest-price) for `SetHoldings` / `FixedNotional`.
5. **PR5 — Coexistence guard.** Symbol-scoped pre-flight refusal (Decision 13); deploy form `blockedReason` integration; connectivity strip's `nowChecks` integration.
6. **PR6 — Per-trade audit list.** `SIZING_RESOLVED` WAL event + projection field (Decision 11); trades query join; Sizing card bottom section.
7. **PR7 — `sizing_surface` registry + explicit-strategy disable.** `StrategyRegistration.sizing_surface`; widened trimmed `EngineStrategyInfo`; deploy form sizing control disabled + "self-sized" label for explicit strategies; order-surface fail-fast (Decision 6).

Decision 8 ("the canary fix is config-only") becomes available end-to-end at the end of PR1. The deliberate behaviour change to `LeanSetHoldingsSizing` is isolated to PR2 with its regression test. Reference parity's all-in foot-gun protection ships in PR5, between Custom and audit. The ordering is justified by **user value per PR**, not by tidiness of subsystem coverage.

### Non-changes to revisit later (named but not v1)

- `FixedShares` long-only / no-accidental-short stays from Decision 4 (no v1 strategy shorts).
- Arbitrary fractional `SetHoldings(0<f<1)` stays deferred (no v1 strategy uses it).
- `spy_vwap_reversion` and `spy_ema_crossover_options` both stay `sizing_surface=explicit` for v1; the equity strategy's literal `100` and the options strategy's `contracts_per_trade` migrate to `policy` in a future PR once operator UX is validated on the equity preset path.
- The `LiveConfig`-dataclass full Pydantic rewrite stays out of scope (Decision 2 unchanged — extend the allow-list narrowly).
- Capital sleeve, risk overlays, ATR/Kelly/vol-targeting stay deferred (Decision 9 unchanged).
