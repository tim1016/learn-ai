# PRD — Strategy Validation is a human flag; the Deploy page re-homes to the Bots menu

- **Surfaces:** `strategy-validation` (`Strategy Lab ▸ Strategy Validation`) and the single Deploy page, re-homed from `Strategy Lab ▸ Deploy` to `Broker ▸ Deploy`, a sibling of `Bots` / Bot Control (`broker/bots/:id`).
- **Builds on:** ADR 0020 (validation as a strategy-level property; Deploy selects only), ADR 0021 (deploy launch-default posture), PRD #917 (Strategy Lab redesign — this PRD revises its nav + validation model).
- **Decision record:** ADR 0023 (this PRD's authority). ADR 0023 **amends** ADR 0020 (§1 gate, §2 signal default, §3–§4 authoring surface, nav).
- **Design source:** 2026-07-05 `grill-me` session (decisions captured here), cross-checked against the live frontend + `qc_reconciler.py`.
- **Data plane:** Python REST reads and refreshes registered engine/reconciliation evidence only. Flag writes are guarded as data-plane-control actions, derive `flagged_by` from the server-side operator context, and store append-only runtime ledger events outside the committed seed manifest. Existing `LiveRunsService` remains the deploy path; GraphQL, if used, transports only and computes no math.
- **Honesty rules (binding):** ADR 0023 (human flag over evidence; always persist evidence + reason), ADR 0011 (paper-only fail-closed; `live` runtime-inactive), `.claude/rules/numerical-rigor.md` (behavioral equivalence needs matching signals/PnL within a documented tolerance plus a reason), timestamps `int64 ms UTC` at every boundary, raw backend identifiers render through the shared `receiptLabel` pipe.
- **Status:** ready-for-agent.

---

## Problem Statement

Today the app conflates *proving a strategy* with *deploying a bot*, and it models validation as something the machine decides.

The Deploy page sits under **Strategy Lab**, but deploying a validated strategy is *creating a bot* — it belongs next to Bot Control in the **Broker/Bots** area, not in the strategy-authoring menu. Meanwhile the Strategy Validation page (ADR 0020) treats "validated" as an automatic property: a strategy is validated iff a port-vs-QC reconciliation *passes*. But "matches well enough" is a judgment. A strict zero-divergence bar never passes a real strategy; any fixed percentage is arbitrary. The person who knows the strategy should look at how our Python engine and the QuantConnect (LEAN) reference actually agree — signals and PnL — and make the call.

Two more leaks: the deploy form treats read-only/paper/live as if execution mode were a property of the strategy, and the validation surface carries deploy-only machinery (asset legs, sizing inputs, readiness gates) that has nothing to do with proving math.

## Solution

Two pages, one job each.

**Strategy Validation** (stays in Strategy Lab) *proves the strategy*. This slice surfaces the registered Python-vs-LEAN/QuantConnect evidence (the backtest ID pins the reference run), refreshes it from the Python-owned manifest projection, shows how their buy/sell entry signals and PnL match — with the `DivergenceCategory` breakdown and a headline match % — and lets a **person** flip a `validated` / `invalidated` flag. There is no automatic threshold that writes the human flag. Every flag write persists an immutable evidence snapshot + a **required reason**, so a 0%-agreement strategy *can* be flagged validated as that person's recorded judgment, but it is not deployable unless the current flag event also carries an accepted behavioral-equivalence verdict tied to a documented tolerance. Validation never trades: no broker, no orders, no readiness gates. Its asset is the safe canary (the signal entity itself); sizing is a 1-share informational readout.

**Deploy** (re-homed to Broker, next to Bots) *creates a bot* from an already-validated, deployable strategy. The strategy dropdown lists only strategies whose latest non-superseded flag event is `validated` **and** whose evidence verdict is accepted for deploy under the behavioral-equivalence rules. Deploy owns everything validation doesn't: deployment name (≠ strategy name), execution mode (read-only / paper / live), connected account, signal stream (defaults to the validated signal, overridable), configurable action-plan legs, configurable sizing, the full readiness gates, and launch options.

The backtest ID is provenance throughout — the thing that pins which QC run was the reference — never the credential itself.

## User Stories

1. As a trader, I open **Strategy Validation** under Strategy Lab, pick a strategy, and refresh its registered evidence — I see our Python engine's PnL and signals next to the QuantConnect/LEAN reference, so I can judge the match myself.
2. As a trader, I see a headline match %, signal-by-signal alignment, and the `DivergenceCategory` breakdown, so I understand *how* they agree or diverge.
3. As a trader, I flip **validated** or **invalidated** myself — the system does not decide for me on a threshold.
4. As a trader, when I flag a strategy I must give a reason, and the system saves the full evidence snapshot with my flag, so my call is on record.
5. As an auditor, I can open any validated strategy and see exactly what the match showed at flag time, who flagged it, when, and why — even when the agreement was poor.
6. As a trader, the Validation page never places an order, never touches a broker, and never shows me engine/broker/account/fleet gates — it only proves math.
7. As a trader, I find the **Deploy** page in the Broker menu next to Bots, because deploying is creating a bot.
8. As a trader, the Deploy strategy dropdown only offers strategies whose latest validation event is both human-validated and accepted for deploy under the documented behavioral-equivalence evidence.
9. As a trader, on Deploy I name the deployment (different from the strategy name), pick the execution mode, and the signal stream is pre-filled with the validated signal but I can change it to any symbol.
10. As a trader, on Deploy I can attach action-plan legs and sizing that differ from the signal entity, and I see the readiness gates and launch options.
11. As a trader, `live` appears as an execution-mode option but is inactive/blocked until the separate live account is wired; read-only and paper work today.

## Design notes / contracts

- **Validation manifest (Python-owned, extended):** per strategy, keep committed seed evidence plus committed `seed_flag_events[]`, then store human writes in an append-only runtime flag-event ledger under ignored service artifacts. The read model projects both sources into current state. Each event is `{ event_id, strategy_key, event_version, flag: validated | invalidated, flagged_by, flagged_at_ms, reason, behavioral_equivalence, evidence_snapshot, evidence_snapshot_sha256 }`. `flagged_by` comes from the server-side operator context; the client never supplies it. `behavioral_equivalence = { verdict: accepted_for_deploy | evidence_only | rejected, detail, tolerance, tolerance_reason, gating_divergence_counts }`.
- **Evidence snapshot (immutable):** every flag event freezes enough refs to reconstruct what the human accepted: `{ settings_file_ref, settings_file_sha256, qc_cloud_backtest_id, audit_copy_ref, audit_copy_sha256, reconciliation_ref, validation_case_symbol, reconciliation_status, diagnostics }`, and the backend verifies `evidence_snapshot_sha256` on load. Large per-trade series may live in content-addressed artifacts later, but their refs and hashes must be part of the event before they can affect deployability.
- **Deployability gate:** the Deploy dropdown gates on the current non-superseded event satisfying `flag == validated && behavioral_equivalence.verdict == accepted_for_deploy`. A 0%-agreement `validated` event remains auditable forever, but it is `evidence_only` or `rejected` and is not deployable unless the numerical-rigor authority is amended in the same change.
- **Validation evidence refresh:** refreshes the registered manifest/reconciliation evidence projection and reloads the reference source; no order path, no broker client is constructed on this surface. A true engine-run trigger is intentionally not faked by this slice.
- **Execution-mode enum:** `read_only | paper | live`, all three defined end-to-end; `live` is rejected at the submission boundary under ADR 0011 until the live-account project.
- **Timestamps:** `int64 ms UTC` at every wire/storage boundary (repo rule).
- **`receiptLabel`:** raw backend identifiers (`DivergenceCategory` codes, gate ids) render through the shared pipe; opaque tokens (backtest ID, paths, hashes) stay verbatim.

## Slices (tracer-bullet, independently shippable)

1. **Nav re-home + one deploy page.** Move Deploy from `Strategy Lab` to `Broker`, sibling to `Bots`; keep exactly one page. (Frontend nav + route; no behavior change.)
2. **Execution-mode enum plumbed three-wide.** `read_only | paper | live` end-to-end; `live` hard-blocked at the submission boundary; read-only + paper active. (Backend + deploy UI.)
3. **Deploy signal-stream default.** Pre-fill signal stream from the validated signal, freely overridable. (Deploy UI + manifest read.)
4. **Validation manifest event-log write path.** Extend validation storage with committed `seed_flag_events[]` plus an append-only runtime flag-event ledger, authenticated `flagged_by`, `flagged_at_ms`, required `reason`, `behavioral_equivalence`, and immutable `evidence_snapshot`; Python data-plane-control write endpoint + read. (Python REST.)
5. **Validation page becomes an action surface.** Refresh registered Python-vs-LEAN/QC evidence (%, signals, `DivergenceCategory`), show the reference source, and expose the human flag control with a required reason. Strip any deploy-only fields; asset fixed to safe canary; 1-share informational sizing. (Frontend + wire to slice 4.)
6. **Deploy dropdown gates on deployable validation evidence.** Only current events with `flag == validated && behavioral_equivalence.verdict == accepted_for_deploy` appear. (Deploy UI + manifest read.)

## Definition of Done

- ADR 0023 merged; ADR 0020 amended; `CONTEXT.md` § validation revised (this PR).
- Each frontend slice passes `npx eslint Frontend/src/ --max-warnings 0` + Vitest; each backend slice passes `ruff check PythonDataService/app/ PythonDataService/tests/` + pytest.
- A validated strategy's evidence snapshot + reason is retrievable and rendered; every flag write is append-only, authenticated, and anchored to code/data/artifact hashes.
- A 0%-agreement flag is accepted and recorded (proof, not prevention), but it is not deployable unless a same-change update to `.claude/rules/numerical-rigor.md` changes the behavioral-equivalence authority.
- `live` is present in the enum but provably rejected at the submission boundary.
- Deploy is reachable only from the Broker menu; the old Strategy-Lab Deploy entry is gone.

## Open questions

- Home of a future validation-run trigger vs Engine Lab's existing `runPython` / `runLean` paths (reuse vs a dedicated endpoint). Until then, the shipped endpoint is explicitly a manifest evidence refresh, not an engine-run facade.
- Whether full per-trade series are embedded in the flag event or stored as content-addressed artifacts; either way, event refs + hashes are required for auditability.
- Multi-account selector on Deploy (deferred by PRD #917; still deferred).

## References

- ADR 0023 (decision record), ADR 0020, ADR 0021, ADR 0011, ADR 0009, ADR 0012.
- `CONTEXT.md` § "Strategy validation & signal stream" → "Revised 2026-07-05".
- `Frontend/src/app/components/strategy-validation/`, `Frontend/src/app/components/broker/broker-deploy-form/`, `Frontend/src/app/shell/app-sidebar.component.ts`.
- `PythonDataService/app/research/parity/qc_reconciler.py` (`DivergenceCategory`).
