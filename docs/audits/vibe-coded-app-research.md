# Vibe-Coded App Research Audit — learn-ai

**Status:** Research complete (research report + findings); operator manual deferred to follow-up tick.
**Date:** 2026-06-14
**Driver:** `docs/audits/vibe-coded-app-research-prd.md`
**Method:** 10-lens multi-agent audit (workflow `wf_def78013-ce4`) + adversarial verify + main-loop synthesis.
**Scope reached:** Frontend (`Frontend/src/app/components/broker/**`, services, types), Backend (`Backend/Services`, `Backend/GraphQL`), PythonDataService (`app/engine/live/**`, `app/broker/ibkr/**`, `app/schemas/live_runs.py`, `app/routers/engine.py`, ADR 0006/0007/0008/0009 code paths), governance docs (`docs/math-sources-of-truth.md`, `engine-authority-map.md`, `numerical-authority-migration-plan.md`, ADR 0001-0009).
**Workflow stats:** 136 agents, ~4.9M tokens, 32 min wall-clock. Adversarial-verify pass mostly failed (StructuredOutput wiring issue) but lens audits returned dense prose summaries; main loop verified the highest-stakes claims by direct code read before filing.
**Existing auto-research state:** unchanged. This ran outside the `auto-research-tick` state machine (which is `build-alpha-validation-complete-awaiting-review`), exactly like the 2026-05-12 ad-hoc ML predictions validation.

---

## 0. Executive Summary

The repo is in materially better shape than its size suggests. ADR 0009's live-sizing rewrite ships almost end-to-end. The timestamp ban-list cluster (F-0009..F-0024, F-0033) is closed. The provenance-block sweep (F-0027) is done. The math-sources-of-truth registry is current with the live-sizing cutover (line 62, dated today). The engine boundaries are correct — Python owns math, .NET is transport, Angular renders — with one residual exception worth flagging (VCR-0005).

The headline finding is that the **most aggressively defended subsystem still has two live back-doors** to the exact failure mode it was built to prevent:

- **VCR-0001 (P0)**: `live_config = {}` deploys cleanly. The engine constructs no `OrderSizer`, falls back to legacy `SimpleFloorSizing`, and `set_holdings(SPY, 1.0)` buys the entire account. The code comment at `live_engine.py:406-408` documents this fallback explicitly. The $250k surprise can recur silently from a config-only mistake (stale frontend cache, CLI / curl caller, or replayed legacy ledger).
- **VCR-0002 (P0)**: ADR 0008's durable-submit + cold-start-reconciler + intent WAL is **fully implemented but never wired into production**. `live_engine.py:1086` reads verbatim "RECONCILE is a runtime no-op — the ColdStartReconciler is the…". `live_engine.py`/`run.py` import none of `ColdStartReconciler`, `IntentWal`, `submit_state_machine`, `reconciliation_classifier`, or `build_order_ref`. The legacy `client_order_id` cache still owns broker idempotency. The relaunch-poisoning bug class ADR 0008 was written to close is open.

Beyond those two, the audit surfaces 11 individually-tracked findings and ~17 rolled-up issues. The most operator-decision-degrading are the **deploy-flow integrity gaps** (VCR-0004 — 7 of 10 advertised strategies cannot import; VCR-0006 — ledger `account_id` never compared to broker `connected_account`) and the **UI-vs-runtime claim mismatches** (VCR-0007 FLATTEN aliases STOP; VCR-0008 RECONCILE is a no-op while the UI shows success; VCR-0010 paper-mode hero hardcoded). Dead-code is moderate (VCR-0017 rollup) — most candidates are clear deletes.

**Total findings: 19 files + P3 rollup**. P0=2, P1=10, P2=6, P3=many (rolled up).
**Dedup against existing F-NNNN**: VCR-0005 extends F-0018; rest are net-new.

| Severity | Count | Headline |
|---|---|---|
| P0 | 2 | `live_config={}` → SimpleFloor → $250k path; ADR 0008 implemented-but-not-wired |
| P1 | 10 | strategy-key bug (7/10 strategies broken); stale-Greek Net surface; ledger account never verified; FLATTEN-aliases-STOP; RECONCILE no-op; emergency-flatten no cancel-first; cross-restart fill desync; paper-mode hero hardcoded; non-constant-time daemon token compare; (VCR-0007 implicit P1 — UI/runtime label mismatch) |
| P2 | 6 | test-mocks mask P1; QC-approved overclaim; migration plan stale; engine-authority-map LEAN sidecar stale; dead-code rollup; UI/runtime claims rollup |
| P3 | ~17 | naming, label, line-drift, polish — see VCR-P3-rollup |

## 1. Findings (by severity)

| ID | Severity | Title | Trading impact | Dedup |
|---|---|---|---|---|
| [VCR-0001](vibe-coded-app-research/findings/VCR-0001-empty-live-config-bypasses-adr-0009-sizing.md) | P0 | Empty `live_config` bypasses ADR 0009 sizing → `SimpleFloorSizing` → $250k path | Silent 100%-equity buy on first signal | none |
| [VCR-0002](vibe-coded-app-research/findings/VCR-0002-adr-0008-durable-submit-implemented-not-wired.md) | P0 | ADR 0008 durable-submit + cold-start reconciler implemented but never wired | Cross-restart broker state corruption | none |
| [VCR-0003](vibe-coded-app-research/findings/VCR-0003-sizing-resolved-wal-event-never-emitted.md) | P1 | `SIZING_RESOLVED` WAL event declared but never written by production | Per-trade audit is a sidecar projection, lacks `intent_id` join | none |
| [VCR-0004](vibe-coded-app-research/findings/VCR-0004-strategy-key-registry-module-name-mismatch.md) | P1 | 7 of 10 deploy-dropdown strategy keys cannot import (registry/module split) | Operator-deploy fails for 7 strategies; ledger poisoned with unresolvable key | none |
| [VCR-0005](vibe-coded-app-research/findings/VCR-0005-portfolio-valuation-stale-entry-greeks.md) | P1 | `PortfolioValuationService` still aggregates stale entry Greeks into `NetDelta/Gamma/Theta/Vega` | GraphQL surface publishes stale Greeks labeled as current; persists in snapshot history | **extends F-0018** (closure was doc-only) |
| [VCR-0006](vibe-coded-app-research/findings/VCR-0006-ledger-account-id-not-verified-against-broker.md) | P1 | `ledger.account_id` never compared to broker's `connected_account` at start | Wrong-account fills if env misconfigured; identity claim broken | none |
| [VCR-0007](vibe-coded-app-research/findings/VCR-0007-flatten-aliases-stop-ui-claims-immediate-close.md) | P1 | `FLATTEN` aliases to STOP; UI promises "Close all open positions immediately" | Operator surprise on panic flatten; no flatten-and-keep-running primitive | none |
| [VCR-0008](vibe-coded-app-research/findings/VCR-0008-reconcile-runtime-noop-ui-claims-action.md) | P1 | `RECONCILE` is a runtime no-op (`live_engine.py:1086` verbatim); UI shows success | Stale view masquerading as fresh; "Fix this" links lie | companion to VCR-0002 |
| [VCR-0009](vibe-coded-app-research/findings/VCR-0009-emergency-flatten-no-cancel-open-orders.md) | P1 | `cmd_emergency_flatten` places liquidation without cancelling open orders | Race: open SELL limit + emergency SELL → double-sell | none |
| [VCR-0010](vibe-coded-app-research/findings/VCR-0010-paper-mode-hero-hardcoded.md) | P1 | broker-instances hero hardcodes "Paper trading mode — no real money at risk" | Future live-mode work would silently keep showing paper banner | none |
| [VCR-0011](vibe-coded-app-research/findings/VCR-0011-non-constant-time-daemon-token-compare.md) | P1 | Host-daemon shared-secret compare uses `!=` (not `hmac.compare_digest`) | Timing side-channel on daemon auth surface (loopback / LAN attack model) | none |
| [VCR-0012](vibe-coded-app-research/findings/VCR-0012-cross-restart-fill-desync-perm-id-orphan.md) | P1 | `_convert_ibkr_fill` drops `perm_id`-bot-owned fills missing from `_order_meta` | Cross-restart position drift; subsequent `set_holdings` against stale view | companion to VCR-0002 |
| [VCR-0013](vibe-coded-app-research/findings/VCR-0013-frontend-vitest-mocks-mask-registry-mismatch.md) | P2 | Vitest mocks return module-name shape that production never emits | Masks VCR-0004 from CI signal | none |
| [VCR-0014](vibe-coded-app-research/findings/VCR-0014-qc-cloud-backtest-id-unverified-labeled-qc-approved.md) | P2 | Provenance card labels operator-typed `qc_cloud_backtest_id` "QC-approved" | Operator trusts a link the system never verified | none |
| [VCR-0015](vibe-coded-app-research/findings/VCR-0015-numerical-migration-plan-stale-no-adr-0009.md) | P2 | `numerical-authority-migration-plan.md` has no ADR 0009 awareness | Three-registry contract broken; future contributor may re-introduce SimpleFloor | extends F-0018 area |
| [VCR-0016](vibe-coded-app-research/findings/VCR-0016-engine-authority-map-lean-sidecar-stale.md) | P2 | `engine-authority-map.md` LEAN sidecar status "Phase 0 ADR only" but Phases 1a..5g shipped | Doc-of-record off by ~9 PRs | none |
| [VCR-0017](vibe-coded-app-research/findings/VCR-0017-dead-bloated-code-and-docs-rollup.md) | P2 | Dead/bloated code rollup (authors/books, run-comparison, validation_study.py, polygon.service, broker-user-manual) | repo bloat; CI noise | none |
| [VCR-0018](vibe-coded-app-research/findings/VCR-0018-ui-vs-runtime-claims-rollup.md) | P2 | UI-vs-runtime claims rollup (sentinel pill mode-blind, stop 2s timeout, ReadinessGate labels, force-flat enforcement, etc.) | Operator-decision degradation | none |
| [VCR-P3-rollup](vibe-coded-app-research/findings/VCR-P3-rollup.md) | P3 | 17 polish items: naming convention papering, ADR line drift, sizing skip log, etc. | nil today | none |

## 2. Trading-impact analysis (P0/P1 only)

| Finding | Today's blast radius | Tomorrow's blast radius |
|---|---|---|
| VCR-0001 | $250k paper account silently buys 100% on first signal | Same path carries to live; one config-only mistake = real money |
| VCR-0002 | Crash-during-submit unrecoverable; cross-restart desync | Multi-account / live drift; relaunch poisoning ADR 0008 was written to close |
| VCR-0003 | Per-trade audit incomplete; sidecar snapshots lose intent_id; cross-policy redeploys lose history | Forensic gap if disputed fill ever needs unwinding |
| VCR-0004 | 7 of 10 dropdown strategies fail at start; ledger poisoned | Operator confusion + redeploy churn; auto-managed fleets stuck |
| VCR-0005 | Stale Greeks in `getPortfolioValuation` GraphQL surface + `PortfolioSnapshot` history; **no current consumer template** | First portfolio-summary tile or risk-cap engine consumes stale Greeks silently |
| VCR-0006 | Wrong-account paper fills on misconfigured env; UI shows wrong account | Same code-path on live = wrong-account live orders |
| VCR-0007 | Operator panic-flatten leaves bot stopped (not just flat); no de-risk primitive | Scheduled square-up workflows misuse the verb |
| VCR-0008 | "Re-sync now" / "Fix this" lie; operator trusts stale view as fresh | Decision-time errors based on staleness |
| VCR-0009 | Open SELL limit + emergency-flatten → double-sell | Same on live |
| VCR-0010 | Hardcoded hero label gives false reassurance | Day a live-mode path lands, banner silently wrong |
| VCR-0011 | Timing side-channel on host-daemon auth | Same |
| VCR-0012 | Cross-restart fill desync; subsequent `set_holdings` from stale base | Silent broker-state corruption |

## 3. Clean areas (real wins worth recording)

The lens audits found a lot of correct, well-defended code. Documenting it makes regressions easier to catch later.

**ADR 0009 surface (the new safe-canary path)**
- `order_sizer.py` exists with the four-kind Pydantic discriminated union, decimal-string coercion, `SizingKindNotWiredError`.
- `LeanSetHoldingsSizing` is wired into `OrderSizer.__init__` with `IbkrEquityCommissionModel` and is invoked from the SetHoldings percent path (PR2 cutover real).
- `_live_config_from_ledger` accepts the `sizing` key, parses through `parse_sizing_policy`, raises on unknown keys.
- `HostRunnerDeployRequest._validate_sizing` validates `sizing` and re-serializes canonically so hash stability survives float-vs-string noise.
- `audit_copy_allow_list.py` implements three-outcome verdict (proven_match / proven_mismatch / cannot_prove) with per-lookup sha re-verification.
- `docs/references/audit-copy-sizing-allow-list.json` exists with both expected entries; both shas verify on disk.
- `build_ledger` computes engine-derived `governed_by` + `sizing_provenance`; defaults fail-closed to `live_override` unless proven_match.
- `_enforce_explicit_surface_policy` at deploy refuses a non-StrategyExplicit sizing for `ema_crossover_options`.
- `StrategyRegistration.sizing_surface` exists; `ema_crossover_options` is `'explicit'`.
- Deploy form renders the three-option radio with inline Reference parity gate; defaults to `safe_canary`.
- `broker-sizing-card` renders the Pre-policy variant honestly when policy is `null`.
- `LivePortfolio.liquidate` bypasses the policy adapter (Decision 4 conformance).
- `check_all_in_coexistence` refuses `SetHoldings(1.0)` on non-flat symbol or sibling-managed collision.
- Per-trade audit row writes survive sidecar I/O failure (PR6 reviewer fix at `run.py:716-721`).
- Sidecar rotation on `run_id` mismatch (`run.py:687-689`).
- FixedShares is long-only in v1: `OrderSizer.resolve_set_holdings_quantity` raises on negative target_fraction.

**Deploy flow**
- Pydantic input validation at `HostRunnerDeployRequest` is thorough (length, ranges, regex-constrained `strategy_instance_id`, decimal-string for SetHoldings.fraction).
- Timestamp wire format consistently `int64 ms UTC` (start_date_ms, created_at_ms, started_at_ms); no ISO/naive datetimes; no ban-list violations in host_daemon / deploy / run / live_runs.
- Money sizing types use `decimal.Decimal` end-to-end via `parse_sizing_policy`.
- `live_config` is fail-closed allow-list at runtime boundary.
- `strategy_instance_id` path-traversal sanitization is consistent between Frontend (INSTANCE_ID_RE) and backend (`validate_strategy_instance_id`), kept in lockstep by a parity test.
- Path-injection guards on `qc_audit_copy_path` and `strategy_spec_path` re-confine under repo_root before any read.
- Host-daemon shared-secret on every actuation route except `/health`.
- Deploy idempotency: identical inputs return `created=false`.

**Halt / pause / stop / poison state machine**
- `halt.py` atomic exclusive-create (`open('x')`) prevents TOCTOU; corrupt flag raises rather than silently treating as absent.
- `command_channel.py` tmp+fsync+os.replace publish; acked_seqs read alongside pending; corrupt command file halts dispatch loop.
- `desired_state.py` durable intent survives crash/reboot keyed by `strategy_instance_id`.
- `pre_flight.py` clean_tree / ntp_offset / unexpected_position / run_state_intact / no_halt_flag / yesterday_artifacts / all_in_coexistence — each returns a `CheckResult`.
- `readiness.py` distinct `live_readiness` vs `start_readiness` kinds; UNKNOWN for missing gates; BLOCKED iff hard FAIL.
- `process_registry.py` SIGTERM-with-timeout then SIGKILL fallback; crash recovery before AlreadyRunningError.
- `broker-paper-run` distinct DESIRED / RUN / PROCESS state pills; poisoned.flag and halt.flag rendered as separate pills.
- `MARK_POISONED` writes a structured schema-validated flag so boot-time parser loads cleanly.
- `emergency-flatten` CLI requires `--confirm`, `--account` match check (refuses on mismatch), paper-only via DU sentinel.

**Broker safety stack**
- Four enforcement layers in `place_paper_order` (IBKR_READONLY, IBKR_MODE=paper, port not in LIVE_PORTS, account_id starts with DU) — runtime cannot accidentally route to a live account regardless of UI choice today.
- `NoSubmitBrokerAdapter` (shadow mode) is structural; no path reaches `ib.placeOrder`.
- `pre_flight.check_unexpected_position` is namespace-aware (managed_symbols vs foreign).
- `halt.write_poisoned_flag` atomic `open('x')`.

**Wire fidelity (post-Phase 3 baseline closure)**
- `intent_events.py` / `intent_wal.py` / `intent_ledger.py` `ts_ms` explicitly Field-bound to int64; fold cursor is `seq` not `ts_ms`.
- `broker/ibkr/bars.py` ingestion-boundary with tz=UTC + `IBKRBarStreamError` on naive datetimes.
- `broker/ibkr/orders.py` `exec_time_ms` separately captured from `Execution.time`, never serialized as datetime.
- `broker/ibkr/models.py` every Pydantic model timestamp field is `int ms`.
- `bar_adapter.py` documented two-boundary conversion (ms → tz-aware ENGINE_TZ → ms).
- `live-runs.types.ts` / `live-instances.types.ts` all timestamp fields typed `_ms: number`, no string DateTime fields.
- Broker FE consistently uses `fmtTimestampNy()` or explicit `DatePipe ':UTC'`.

**Run-ledger / provenance**
- `compute_run_id`: 7 fields hashed in `canonical_json` with `sort_keys=True` nested-dict-stable.
- `governed_by` + `sizing_provenance` derived in `build_ledger`; deploy schema does NOT accept them (no operator-injection path).
- `LiveRunLedger` Pydantic model has `extra="forbid"`.
- Sizing card renders pre-policy runs honestly (policy=null).
- Provenance card surfaces all 7 hashed fields in proof rows + full-fingerprint disclosure.
- `InstanceProvenance` backend builder uses safe defaults for legacy ledgers.
- `strategy_key` foot-gun guard rejects `--strategy` mismatched against `ledger.strategy_key` for non-legacy ledgers.
- `Decimal`-on-the-wire: SetHoldings.fraction + FixedNotional.value coerced to Decimal at SizingPolicy boundary; forbids float on wire.

## 4. High-suspicion areas needing deeper review

The audit was time-boxed; these are areas where the surface read was confident but a deeper inspection is warranted.

1. **FLATTEN dispatcher and `_shutdown_flatten`** — VCR-0007 surfaced from lens summary prose; the specific line ranges were not re-verified by the main loop. Confirm exactly which path persists durable STOPPED and exactly what `_shutdown_flatten` cancels before liquidating.
2. **`cmd_emergency_flatten`** — VCR-0009 likewise from prose; verify the function body and compare against `recovery_flatten` / `force_flat` to confirm the asymmetry.
3. **`_convert_ibkr_fill`** — VCR-0012 surfaced from prose; verify the gate condition and what happens when `perm_id` is known but `order_id` is absent.
4. **`daemon_auth.py` token compare** — VCR-0011 needs the literal line.
5. **`broker-instances` hero label component** — VCR-0010 needs the specific component / line.
6. **Sentinel pill, stop 2s timeout, ReadinessGate labels** (VCR-0018 § A, B, C) — surfaced from prose; each item should be re-grounded.
7. **`_fatal_halt` and `_persist_desired_state` exception handlers** (VCR-0018 § G) — confirm the swallow-on-write pattern and what happens to the next start.
8. **TOCTOU race in `RunnerProcessManager.start`** (VCR-P3 § P) — verify the lock granularity.
9. **`PortfolioValuationService` MarketValue path** — VCR-0005 only addresses the Greeks aggregation; the MarketValue aggregation is registry-blessed as "compliant" but warrants a fresh look once Greeks are migrated.
10. **`reconcile.py` / `cold_start_reconciler.py` ownership-precedence logic** — both modules are correct in isolation per the lens read, but the production wiring is absent (VCR-0002); when wiring lands, the wiring contract itself needs adversarial review (where to invoke from, transaction boundaries, partial-fold safety).

## 5. Dead / bloated code and docs inventory

See [VCR-0017](vibe-coded-app-research/findings/VCR-0017-dead-bloated-code-and-docs-rollup.md). Summary:

| Candidate | Action | Confidence |
|---|---|---|
| Frontend `authors/` + `books/` scaffold subtree | delete | high |
| Frontend `run-comparison/` (dead sibling of `runs-compare`) | delete | high |
| Frontend `polygon.service.ts` | delete | high |
| Python `routers/validation_study.py` (1054 lines, unregistered) | delete or owner-review | medium |
| `docs/broker-user-manual.html` + `.pdf` | archive after new operator manual ships | high |
| Root-level scratch files (6 of them) | archive or delete after owner | medium |

Not dead, despite suspicion: `lean-script-editor`, `ide-sandbox`, `data-quality-docs`, `runs-compare`, `BacktestService`/`SpecStrategyService`/`ResearchService`/`SanitizationService`, all phase docs.

## 6. Dependency-ordered remediation plan

Sequence chosen to (a) close P0 bypass paths first, (b) close UI/runtime mismatches that operators see daily, (c) finish the ADR 0009 wiring, (d) close governance drift, (e) cleanup.

### Stage A — Close the two P0s (block-everything-until-done)

1. **VCR-0001**: make `live_config.sizing` required at the schema level (typed `LiveConfigModel`) OR refuse `LivePortfolio` construction without a policy when ledger schema version is post-PR1. Pair with a pre-flight gate `check_sizing_policy_present`. Tests: empty `live_config` → 400 at API boundary, not 200-then-silently-Simple-Floor.

2. **VCR-0002**: multi-PR wiring of ADR 0008. Sequence:
   - PR α: `IntentWal.append` on every order placement (drop legacy `_IDEMPOTENCY_CACHE`).
   - PR β: stamp `order.orderRef = build_order_ref(...)` on every `IB.placeOrder`.
   - PR γ: invoke `ColdStartReconciler.verify()` from `cmd_start` before any signal can produce an order.
   - PR δ: register a `VerifiedBrokerOwnershipQuery` subclass for IBKR so `broker_ownership_query.require_durable_submit_activation` admits live callers.
   - PR ε: add `submit_state_machine.next_action` to the submit retry loop.
   - PR ζ: move `reconcile.py` to delegate to `reconciliation_classifier.classify`.
   - PR η: companion fix for VCR-0012 (`_convert_ibkr_fill` consults classifier).

   Until that work lands, gate the cockpit's "Live & trading" verdict with a "ADR 0008 not wired — restart requires manual reconciliation" banner.

### Stage B — Close the deploy-flow integrity gaps

3. **VCR-0004 + VCR-0013** (joint): pick canonical strategy key form (registry-key OR module-name). Align `_STRATEGY_REGISTRY`, the runner's `import_module`, the Frontend mocks, the `FALLBACK_STRATEGY` constant, and the spec-fixture auto-fill. Add the contract test `set(s.name for s in /api/engine/strategies) == set(_STRATEGY_REGISTRY.keys())` and the integration test that walks every registered key through `import_module`.
4. **VCR-0006**: add `account_id` comparison in `_validate_paper_client` (or in `cmd_start`) before `engine.run()`. Mirror the pattern in `cmd_emergency_flatten`. Add forensic `session_started` event capturing both `ledger.account_id` and `connected_account`.

### Stage C — Close the UI-vs-runtime claim mismatches

5. **VCR-0008**: change the verb's UI label to "Mark for re-sync at next restart" until the runtime reconcile path is wired (VCR-0002 PR γ). Remove "Fix this" routes that point to RECONCILE for gates that need runtime mutation.
6. **VCR-0007**: add a true FLATTEN-without-STOP primitive, OR rename the UI to "Stop and close all open positions". Add confirmation modal distinguishing the two outcomes.
7. **VCR-0009**: add `await broker.cancel_open_orders(owned_only=True)` as the first step of `cmd_emergency_flatten`.
8. **VCR-0010**: bind the broker-instances hero to a server-side mode verdict (structured `{mode, port_class, readonly, account_prefix}`). Same pattern for VCR-0018 § A sentinel pill.
9. **VCR-0014**: split the Provenance card proof row into "Audit copy verified" + "Operator-recorded QC backtest" (the second labeled as not auto-verified).

### Stage D — Finish ADR 0009 wiring

10. **VCR-0003**: emit `SIZING_RESOLVED` WAL event from `OrderSizer.resolve_set_holdings_quantity` carrying `{intent_id, policy_kind, policy_value, intended_qty, reference_price, sizing_provenance_at_resolve_time, sized_via}`. Migrate Sizing card query to read from the WAL.
11. **VCR-P3 § E**: emit "sizing skip" log on zero-share resolution.
12. **VCR-P3 § F**: extend `LivePortfolio.market_order` to consult `registered_sizing_surface` (close the reverse-direction fail-fast gap).
13. **VCR-0011**: switch host-daemon token compare to `hmac.compare_digest`.

### Stage E — Close governance drift

14. **VCR-0015**: add "Phase 5 — Live-sizing migration (ADR 0009)" to `numerical-authority-migration-plan.md`. Update sequencing summary table. Correct the Phase 2 row (only PortfolioRiskService migrated).
15. **VCR-0005**: finish Phase 2.3 — delete the stale-Greek aggregation in `PortfolioValuationService.cs::ComputeValuationInternal`. Drop `NetDelta/Gamma/Theta/Vega` from the returned `PortfolioValuation` and `PortfolioSnapshot` until a consumer needs them. Update F-0018 closure note.
16. **VCR-0016**: update `engine-authority-map.md` row 19 + `lean-sidecar-lab.md:3` status header.

### Stage F — Cleanup

17. **VCR-0017** § A,B,D: single delete-sweep PR for high-confidence dead code.
18. **VCR-0017** § C, H: owner-review PR for medium-confidence items.
19. **VCR-0018** § C, E, G: structured ReadinessGate label coverage; rename Historical Data Loading label; re-raise on `_fatal_halt` write failure.
20. **VCR-P3-rollup**: opportunistic polish sweep.

### Stage G — Build the operator manual

21. Run the deferred manual-tick (`docs/operator-architecture-and-runbook.md`) per PRD §6.3.
22. Migrate unique safety / pre-flight / troubleshooting content from `broker-user-manual.html` into the new manual.
23. Archive `broker-user-manual.html/.pdf` after the migration.

## 7. Provenance of the audit

Run as a one-off research workstream outside the `auto-research-tick` state machine (which remains `build-alpha-validation-complete-awaiting-review`). Pattern mirrors the 2026-05-12 ad-hoc ML predictions validation run.

- Workflow `wf_def78013-ce4`, script at `docs/audits/vibe-coded-app-research-workflow.mjs`, run summary at `docs/audits/auto-research/runs/2026-06-14-vibe-coded-app-research.md`.
- 10 lens audits: live-sizing-adr-0009, live-deploy-flow, strategy-registry-key-mapping, halt-pause-stop-flatten-poison, broker-order-ownership-reconcile, run-ledger-identity-provenance, timestamp-wire-contracts-runtime, architectural-drift-registries, dead-bloated-code-docs, ui-vs-runtime-claims.
- Adversarial verify pass mostly failed due to `StructuredOutput` tool wiring inside the verify subagent — the structured `surviving_findings` array captured 12 verified findings; the lens prose summaries contributed the rest. Main loop verified every P0 claim by direct code read (`live_engine.py:402-418`, `live_engine.py:1086`, `intent_events.py:46`, `_STRATEGY_REGISTRY` listing, `broker/ibkr/orders.py:49-73`) before filing.

## 8. Dedup notes

- **F-0018** (closed) on migration-plan vs registry Phase 2-3 drift: VCR-0005 and VCR-0015 both extend. F-0018 was closed by editing only documentation; the underlying code violation in `PortfolioValuationService.cs` remains.
- **F-0023** (closed) dataset-service forward-fill gaps: no overlap.
- **F-0026** (deferred): fixture coverage gap, multi-week; out of scope here.
- **F-0027** (closed) provenance blocks: ADR 0009 surface confirmed provenance blocks present; no new P1.
- **F-0034** (deferred P2): frontend naive `Date` parse rollup; the live-trading wire path (this audit's scope) is clean. VCR-P3 § J / K identify new manifestations specific to recent cockpit additions, not F-0034 reopenings.
- **F-0035 / F-0036** (closed) frontend snake_case bugs: distinct from the registry-key issue (VCR-0004).
- **ML-V-001..004**: distinct from this audit's scope (ML predictions vs live trading).
- **BA-V** (Build Alpha validation): distinct.

No existing F-NNNN was reopened. All VCR findings are net-new manifestations.

## 9. What this audit did NOT cover

Explicit ungated areas, by lens:

- End-to-end integration tests on the live-sizing back-door (VCR-0001) — finding is static-only; the code path is unambiguous but no `pytest -k` was exercised.
- IBKR adapter internals beyond the order-placement / fill surface.
- Cold-start-reconciler / reconcile semantic correctness when wired (the modules read correctly in isolation; their wiring contract is future work).
- Fleet / cross-account coordination beyond `_sibling_all_in_symbols`.
- `broker-options-surface` 3D rendering claims (out of P0/P1 trading scope).
- Backend.Tests dead test classes (time-boxed out).
- Live runtime behavior under sustained multi-day operation (no clock-shift testing).
- Per-component runtime route probes (defaults agreed: static refs only).

## 10. Operator manual (deferred)

Per the staging decision, the operator manual (`docs/operator-architecture-and-runbook.md`, PRD §6.3) is deferred to a follow-up tick. The audit findings now inform what the manual must call out as dangerous. Stage G of the remediation plan covers the manual production.

---

*Generated by the vibe-coded-app-research workstream, 2026-06-14. Findings: `docs/audits/vibe-coded-app-research/findings/`. Run summary: `docs/audits/auto-research/runs/2026-06-14-vibe-coded-app-research.md`.*
