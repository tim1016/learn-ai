---
id: VCR-P3-rollup
severity: P3
status: partially_remediated
area: cleanup
canonical_file: multiple
reference: PRD §7
first_seen: 2026-06-14
last_seen: 2026-06-14
remediation_progress:
  - "#494 — Phase 2 — P3-A DeploymentValidationAlgorithm alias retired via explicit class_name"
  - "#494 — Phase 2 — P3-B unregistered modules gated by registry-based runner check"
  - "#494 — Phase 2 — P3-C spec-fixture auto-fill follows module-name keys"
  - "#501 — Phase 6D — P3-P/Q per-instance start lock + halt.flag pre-flight rerun"
  - "P3-E — Phase 8 SIZING_SKIP audit log shipped via #544 (sizing_skip.jsonl; symbol/policy/target/current/reason captured)"
  - "P3-F — Order-surface reverse fail-fast: LivePortfolio.submit_market_order accepts explicit_call=True from ctx.market_order / strategy.market_order; raises Order-surface mismatch when registered_sizing_surface == 'policy'. The forward case (explicit + set_holdings) was already in place at live_portfolio.py:524-530."
follow_up_required:
  - "P3-D ADR 0009 References line numbers — re-anchor on next ADR 0009 touch (doc-only drift; semantics preserved)"
  - "P3-J/K/L/N timestamp rigor + QC card tail — Phase 7 follow-ups, not gating safety"
  - "P3-M sizing policy presence in cmd_start — covered by Phase 1 + Phase 6D start-gate"
  - "P3-O/N dead-code rollup — Phase 11 / rolled into VCR-0017"
lens: multiple
dedupe_with_F: none
confidence: mixed
---

## What

P3 polish / cleanup findings rolled up from across the 10 audit lenses. Each is a low-risk doc / naming / convention issue. None has trading impact today; cleanup is bookkeeping.

### P3-A — `DeploymentValidationAlgorithm` alias is the only thing keeping `deployment_validation` working

`run.py` derives the strategy class name via `<PascalKey>Algorithm` convention. `deployment_validation.py:227` has `DeploymentValidationAlgorithm = DeploymentValidationConsecutiveGreen` as a module-level alias that papers over the naming-convention mismatch. The alias is test-protected by `test_live_start_convention_alias_resolves_strategy_class` so silent removal would fail CI. Loud failure mode (rc=2 at start) — not silent corruption.

**Fix**: either rename the class to `DeploymentValidationAlgorithm` and delete the alias, or have the runner consult `_STRATEGY_REGISTRY[key].build` directly instead of relying on a name convention.

### P3-B — `buy_and_hold` + `spy_vwap_reversion` unregistered but importable by direct `--strategy` flag

Two algorithm modules exist in `algorithms/` and are not registered in `_STRATEGY_REGISTRY`. The dropdown can't reach them, but the runner does not consult the registry — `import_module(f"app.engine.strategy.algorithms.{args.strategy}")` is the only gate. A determined operator with shell access (or a hand-edited ledger) could deploy them. Both are intentional cross-engine reconciliation primitives per their docstrings, not deployable algorithms.

**Fix**: either move them to an `algorithms/reference_samples/` subpackage, or add a `__DEPLOYABLE__ = False` class marker that `run.py` consults before importing, or simply have `run.py` enforce `args.strategy in <module-name set derived from _STRATEGY_REGISTRY>`.

### P3-C — Spec-fixture auto-fill in deploy form broken for registry keys

The deploy form's auto-fill effect does `fixtures.find(f => f.name === strategy)` where `strategy` is the registry key (`ema_crossover`) but `fixtures[].name` is the file stem (`spy_ema_crossover`). The auto-fill never matches for any strategy whose registry key differs from the spec filename. Resolves as a side-effect of fixing VCR-0004.

### P3-D — ADR 0009 References-section line numbers drifted

ADR 0009's "References" section pins load-bearing code claims to file:line ranges, but the PR1-7 catch-up shifted the lines by 4-30. Semantics unchanged; numbers stale.

**Fix**: re-anchor on next ADR touch, or add a one-line note "line numbers anchored to pre-PR1 state; current code preserves the cited semantics".

### P3-E — ADR 0009 § 4 "sizing skip log" not emitted

ADR § 4 specifies that a policy resolving to zero shares while flat should log a "sizing skip" so the operator sees why no entry fired. The current `OrderSizer.resolve_set_holdings_quantity` returns `None` silently in that case.

**Fix**: emit a `live_engine.logger.info("[SIZING SKIP] …")` with the policy, intended_qty=0, reference_price, and reason. Wire into the Sizing card session counter.

### P3-F — `governed_by` not runtime-validated for reverse case

ADR Decision 6's "runtime-validated against the actual order surface" fires only for `set_holdings` calls on `"explicit"`-registered strategies. The reverse case — a `"policy"`-registered strategy using `market_order` while the ledger says `governed_by=live_config` — is not caught. Both halves of the contract should fail-fast.

**Fix**: extend `LivePortfolio.market_order` to consult `registered_sizing_surface` and refuse with `OrderSurfaceMismatchError` when the strategy is registered as `"policy"` but the call type is `market_order`.

### P3-G — Shadow invariant not asserted at runtime

`NoSubmitBrokerAdapter` has structural invariants (no code path reaches `ib.placeOrder`) but those invariants are not asserted at runtime by any check that runs in the production loop.

**Fix**: add a startup assertion: when `BROKER_MODE=shadow`, `assert isinstance(broker, NoSubmitBrokerAdapter)`. Add a smoke test that drives a `set_holdings` through the shadow path and asserts no `IB.placeOrder` mock is invoked.

### P3-H — `_order_belongs_to_account` defense-in-depth weak for multi-client gateways

The current check is name-based and assumes a single client per gateway. With multi-client connections (multiple `client_id` on the same Gateway), the check can match an order belonging to another client.

**Fix**: tighten to `account_id AND client_id` matching. Add a multi-client integration test fixture.

### P3-I — Chart-snapshot endpoint UTC day boundaries vs America/New_York trading

The chart-snapshot endpoint uses UTC for day boundaries while trading is `America/New_York`. Small display anomaly: the day's bars at the boundary appear off-by-one in certain UTC offsets.

**Fix**: convert the boundary in the endpoint to `America/New_York` before computing the day window.

### P3-J — Sizing card per-trade audit `ts_ms` rendered without timezone

The Sizing card displays per-trade audit `ts_ms` via Angular `DatePipe` with no timezone argument, so it defaults to browser-local. Every other broker UI timestamp uses explicit `America/New_York` via `fmtTimestampNy()`.

**Fix**: use `fmtTimestampNy(ts_ms)` like the rest of the broker UI.

### P3-K — `FailureRecord`/`FailureRow.ts_ms` misleadingly named

The field is named `ts_ms` (implying int64 ms UTC) but is actually a host-local-TZ string parsed *as if* UTC at ingestion. Documented in the type comment but not surfaced on the wire shape.

**Fix**: rename to `ts_local` and convert at ingestion to a true `ts_ms_utc`. Or convert at the source so the name matches.

### P3-L — `executions.parquet ts_ms` uses wall-clock observation time, not broker `exec_time_ms`

The fill's `ts_ms` column is the engine's wall-clock at receipt, not the broker's reported execution timestamp. For latency analysis this matters; both should be present.

**Fix**: persist both `received_at_ms` and `exec_time_ms`. Update reconciliation joins to use `exec_time_ms`.

### P3-M — `HostRunnerDeployRequest.live_config` open dict allows undeployable ledger

Operator can inject unknown keys via `live_config` that hash into `run_id`, but the start-gate then refuses with "unknown live_config keys". The deploy succeeds and writes a ledger; the runner exits 2. The operator has to re-deploy under a fresh `run_id`.

This is the deliberate forward-compat design (per `_live_config_from_ledger` docstring: *"Unknown keys are rejected — they'd indicate the ledger was written with a newer schema than this code understands"*). Cleanup polish only: either tighten `_validate_sizing` to symmetrically reject unknown sibling keys at the deploy boundary, or surface the known_fields allow-list in the deploy schema's docstring so operators see the contract.

### P3-N — Stub components without tracker link

`strategy-finder-stub` and `volatility-stub` are routed COMING-SOON placeholders. Add a tracker link or ETA so an operator knows whether the feature is shipping or vestigial.

### P3-O — Root-level scratch / binary clutter

(Also called out in VCR-0017 § H.) `crudops.sql`, `order_store.sql`, two stale `.docx`, `dependency-audit.xlsx`, `247-Critical-feedback.md`, `analysis-hardening-gap-report.docx` — root-level scratch. Either archive under `docs/archive/scratch/` or delete after owner review.

### P3-P — TOCTOU race in `RunnerProcessManager.start`

Two concurrent start requests for the same instance could both spawn live subprocesses if they pass the "already-running?" check between each other's lock acquire. Per-instance lock acquisition wraps the check, but the lock granularity is per-call and the registry refresh races with the spawn.

**Fix**: serialize start requests per `strategy_instance_id` via a per-instance asyncio lock held across the entire check + spawn + register window.

### P3-Q — NTP/clean-tree checks not auto-invoked by `cmd_start`

Only `check_unexpected_position` and `check_all_in_coexistence` are wired inline in `cmd_start`. The other pre-flight checks (NTP offset, clean tree) run only at deploy; a clean deploy → wait → start could see the working tree drift between deploy and start without the gate firing again.

**Fix**: optionally re-run a subset of pre-flight at `cmd_start` (NTP, clean tree, halt flag) so the gate is fresh.

## Why this severity

PRD §7 P3: cleanup / polish / naming / low-risk doc. None of these has trading impact today; cleanup is bookkeeping. Several (P3-E, P3-F, P3-G, P3-Q) are non-trivial code changes but the absence of the change does not currently break correctness.

## Suggested resolution

Pick up opportunistically as related areas are touched. P3-A, P3-D, P3-J, P3-K, P3-O, P3-N can be a single "polish" sweep PR. P3-B, P3-E, P3-F, P3-G, P3-P, P3-Q are individually small but should each carry a regression test.

## Provenance of the findings

Sources: lenses `strategy-registry-key-mapping`, `architectural-drift-registries`, `live-sizing-adr-0009`, `broker-order-ownership-reconcile`, `live-deploy-flow`, `halt-pause-stop-flatten-poison`, `timestamp-wire-contracts-runtime`, `dead-bloated-code-docs`, `ui-vs-runtime-claims` (workflow `wf_def78013-ce4`).
