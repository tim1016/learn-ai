# ADR 0020 — Strategy validation is a strategy-level property authored on a dedicated Strategy Validation page; Deploy only *selects* a validated strategy

**Status:** Proposed 2026-07-05. Drafted during the 2026-07-05 `grill-with-docs` session ("I don't see the deploy page in navigation — add it, and revisit the deploy strategy page"); vocabulary in `CONTEXT.md` § "Strategy validation & signal stream (sharpened 2026-07-05)". **Supersedes** the "The Deploy a strategy page owns creating *or selecting* this package" clause of `CONTEXT.md` § "Validated strategy package".

**Decision drivers:** The live deploy form (`Frontend/src/app/components/broker/broker-deploy-form/`) makes a trader hand-type three technical-provenance values *per deployment*: a repo-relative settings-file path (`specPath`), a QuantConnect backtest ID (`qcBacktestId`), and an audit-copy path (`qcAuditCopyPath`). Yet `CONTEXT.md` already says "raw settings-file paths are technical provenance, not a normal trader input." The evidence that *would* let those fields auto-populate already exists — every `run_ledger.json` carries `qc_cloud_backtest_id` + `qc_audit_copy_path`/`sha256` + `strategy_spec_path`/`sha256` (e.g. `deployment_validation` → `qc_cloud_backtest_id d2fe45a7142e88575f6fbd75229f8681`), the QC algorithm sources live under `references/qc-shadow/`, and the port-vs-reference reconciliations live under `docs/references/reconciliations/`. It is scattered across execution artifacts, never surfaced as a first-class *validated strategy*, and there is no stored `strategy → backtest` mapping to drive auto-population. Meanwhile the operator's mental model is simpler than the form: "a strategy is validated once, against one golden-fixture symbol; then I deploy it."

**Related:** ADR 0012 (strategy-as-signal-generator / action plan as deploy-time instrument declaration — this ADR sits directly on top of that seam), ADR 0016 (bot-control trader-authored activity and deploy packages — origin of the "Validated strategy package" concept), ADR 0009 (live sizing authority — sizing stays a deploy-time binding, *not* part of validation), `.claude/rules/numerical-rigor.md` § "Golden fixtures" (one fixture generated from the reference, pinned). `CONTEXT.md` §§ "Strategy validation & signal stream", "Validated strategy package".

## Context

The repo's founding philosophy is: port math from a reference (QuantConnect/LEAN), prove numerical equivalence against a golden fixture, save the audit copy, then vanish the runtime dependency. A "validated strategy" is exactly that act applied to a whole trading strategy: our LEAN-engine port is reconciled to a QuantConnect Cloud backtest, and the QC algorithm source is checked in under `references/qc-shadow/` as the audit copy.

Three shaping facts emerged during grilling:

1. **Validation is a one-step, strategy-level property — not per-symbol.** A strategy is validated once, against a single **validation-case symbol** (SPY) that serves as its golden fixture. That proves the *port* is faithful. We do **not** demand a fresh backtest per symbol. The live artifact record already proves this is the working pattern: the single backtest ID `d2fe45a7…` is reused across dozens of `run_ledger.json` files pointing at SPY, AAPL, NVDA, TSLA, DIA, and SPY option spreads.
2. **The signal stream is completely independent of the strategy.** The symbol a deployed bot reads (`live_config.symbol`) is a free deploy-time choice; the validation-case symbol neither defaults, constrains, nor warns it. (Distinct again from the *traded instrument*, which the action plan controls — ADR 0012.) All three symbols may differ.
3. **The evidence is not new data — it needs consolidating and promoting to a first-class record.** A strategy must be able to be *validated without ever having been deployed*, so the source of truth cannot be "scan the run ledgers."

The prior documented boundary put package authoring *inside* Deploy. Grilling moved it out: authoring is a reusable, browse-and-register surface; Deploy is a fast selection surface.

## Decision

### 1. Validation is a binary, strategy-level property, pinned to one golden-fixture symbol

A strategy is **validated** iff it has a stored QC backtest ID, a saved audit copy (`references/qc-shadow/<Algo>.py`), and a passing port-vs-QC reconciliation against its single golden-fixture symbol. Validation is not parameterized by deployment symbol. The golden-fixture symbol is **provenance only**.

### 2. The signal stream is an independent deploy-time binding

`live_config.symbol` is chosen at deploy time and is decoupled from both the validation-case symbol and (per ADR 0012) the traded instrument. The deploy flow's Signal Stream tab offers a free symbol choice; deploying on a symbol other than the validation case is *not* an error and carries at most an informational provenance note.

### 3. Authoring split — a dedicated Strategy Validation page owns "becomes validated"; Deploy only *selects*

A new standalone **Strategy Validation page** (nav: `Strategy Lab ▸ Strategy Validation`) is the surface where a strategy *becomes* validated (binding backtest ID + audit copy + reconciliation to the strategy) and where the evidence is browsed. It is a master-detail catalog — a row per strategy, click-through to a detail modeled on the Golden Fixtures surface. The **Deploy flow no longer authors anything**: its first tab is a *selector* of validated strategies that auto-populates settings file, backtest ID, and audit copy (all read-only), with a "View full validation →" link out. This **supersedes** `CONTEXT.md` § "Validated strategy package" ("Deploy owns creating or selecting"): Deploy now only selects.

### 4. A backend-owned validated-strategy manifest is the source of truth — seeded, not re-derived

The `strategy → {settings-file ref/sha, QC backtest ID, audit-copy ref/sha, reconciliation-verdict ref, validation-case symbol}` binding is persisted as a **backend-owned validated-strategy manifest**, seeded from the data that already exists (run-ledger bindings, qc-shadow attribution, reconciliation reports). It is **not** re-derived live from `run_ledger.json` — a run ledger is an *execution* artifact, and a strategy must be validatable without ever having been deployed. Auto-population on the deploy form reads this manifest; nothing is re-typed.

### 5. Validation is the gate to deployability

The Strategy Validation catalog lists **all** strategies with a validation state: **validated** (deployable) vs **unvalidated** ("needs validation" → not deployable). Only validated strategies appear in the Deploy flow's strategy dropdown. Adding validation to an unvalidated strategy flips it deployable.

### 6. The detail surface renders the reference, never the port

The validation detail shows the strategy's brief metadata, the reconciliation diagnostics (trades-matched / trades-validated counts, P&L-matched magnitude, validation-case symbol, and the `DivergenceCategory` taxonomy from `qc_reconciler.py`), and the **QuantConnect reference code** inline. It never renders our internal LEAN/engine port source — sovereignty means the reference is shown for audit, the port is not.

## Consequences

### Positive

- Traders stop hand-typing technical provenance; the "raw settings-file paths are technical provenance, not a normal trader input" rule is finally honored end-to-end.
- Validate-once / deploy-many: one validated strategy fans out to many independent signal-stream / action-plan / sizing deployments — matching the pattern the run ledgers already exhibit.
- A single first-class record answers "how do I know this number is right?" for a whole strategy, consolidating three scattered evidence sources.
- Deploy gets faster and safer: the strategy dropdown is *by construction* only deployable strategies.

### Negative / costs

- A new persisted artifact (the validated-strategy manifest) plus a one-time seeding migration from run ledgers / qc-shadow / reconciliations.
- A new page (list + detail) and a re-shaped deploy tab-1 selector; the manual `specPath` / `qcBacktestId` / `qcAuditCopyPath` inputs are removed from the normal path (a manual-override escape hatch may remain for un-manifested strategies, clearly marked).
- The "register a strategy as validated" flow on the validation page is net-new UI (what evidence a user attaches, and how the reconciliation verdict is computed/refreshed, is left to the PRD).

### Non-consequences

- No change to how reconciliation itself is computed (`qc_reconciler.py`, the `reconcile-backtest` skill) — this ADR *surfaces* it, it does not re-author it.
- No change to ADR 0012's signal/instrument seam; this ADR adds the validation-symbol/signal-stream independence on top of it.
- Sizing stays a deploy-time binding (ADR 0009), never folded into validation.

## Anti-patterns this ADR rejects

- Hand-typing a settings-file path, backtest ID, or audit-copy path at deploy time.
- Deriving the set of "validated strategies" live from `run_ledger.json` (an execution artifact is not a validation source of truth).
- Per-symbol validation gates (demanding a fresh backtest for every deployment symbol).
- Rendering our internal port source on the validation page (only the QC reference is shown).
- Letting an *unvalidated* strategy reach the deploy dropdown.

## References

- `CONTEXT.md` §§ "Strategy validation & signal stream (sharpened 2026-07-05)", "Validated strategy package" — decision/vocabulary record.
- `references/qc-shadow/` (`SpyEmaCrossoverAlgorithm.py`, `DeploymentValidationAlgorithm.py`, `backtests/…/attribution.md`) — the audit copies + fixture attribution.
- `PythonDataService/app/engine/strategy/spec/fixtures/` — the settings files (specs).
- `PythonDataService/app/research/parity/qc_reconciler.py` — `DivergenceCategory` reconciliation taxonomy.
- `docs/references/reconciliations/` — the port-vs-reference reports the detail view surfaces.
- `Frontend/src/app/components/broker/broker-deploy-form/` — the form this ADR re-shapes into a selector.
- ADR 0012, ADR 0016, ADR 0009; `.claude/rules/numerical-rigor.md`.
