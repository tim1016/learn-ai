# ADR 0023 — Strategy validation is a human flag over Python-vs-QuantConnect engine-match evidence; the Deploy page re-homes to the Bots menu and owns all execution-mode selection

**Status:** Proposed 2026-07-05. Drafted during a 2026-07-05 `grill-me` session revisiting the Strategy-Validation / Deploy split from ADR 0020 and PRD #917. (ADR number 0022 was already claimed by the temporal-authority work; this is 0023.) **Amends ADR 0020** (§1 validation gate, §2 signal-stream independence, §3–§4 authoring surface, and its `Strategy Lab ▸ Deploy` nav placement) and the nav decision in PRD #917. Vocabulary: `CONTEXT.md` § "Strategy validation & signal stream" → "Revised 2026-07-05" block. PRD: `docs/architecture/strategy-validation-deploy-rehome-prd.md`.

**Decision drivers:** ADR 0020 modeled "validated" as an automatically-derived, binary property — a strategy is validated iff it carries a stored QC backtest ID + audit copy + a *passing* port-vs-QC reconciliation — and made the Strategy Validation page a browse-only view of a backend-seeded manifest. Two things were wrong in practice. First, the human flag itself is not automatable at the fidelity we can honestly promise: whether a strategy "matches well enough" is a judgment about signal-alignment and PnL closeness that a human makes by looking at the two engines' output. The system must not use an arbitrary match-percentage threshold to write that judgment. Deployability, however, still remains tied to `.claude/rules/numerical-rigor.md`'s behavioral-equivalence requirement: matching signals/PnL within a documented tolerance plus a reason. Second, the operator's model of *where* deployment lives changed: deploying a validated, deployable strategy is *creating a bot*, so the Deploy surface belongs next to Bot Control in the Bots/Broker area, not in Strategy Lab — and the read-only/paper/live execution choice is a *deployment* concern, never a property of validation.

**Related:** ADR 0020 (the strategy-validation / deploy-selects split this amends), ADR 0021 (deploy launch-default posture — unchanged; the execution-mode *default* lives there), ADR 0011 (broker safety verdict / paper-only fail-closed — the reason `live` stays runtime-inactive), ADR 0009 (sizing is a deploy-time binding), ADR 0012 (signal generator / action-plan legs), `.claude/rules/numerical-rigor.md` § "Equivalence levels" (this is the *behavioral* level: matching signals/PnL within a documented tolerance, with the human reason persisted on the flag event), `PythonDataService/app/research/parity/qc_reconciler.py` (`DivergenceCategory`).

## Context

ADR 0020 was right that validation is a strategy-level property proven against a QuantConnect reference, and that Deploy should *select* rather than author. This ADR keeps both. It changes three things a `grill-me` session sharpened:

1. **The flag is human, not automatic.** The Validation page surfaces registered Python-vs-LEAN/QC evidence (QuantConnect is the LEAN reference; the QC backtest ID pins which reference run is used) and displays how well their buy/sell entry signals and PnL match, including the `DivergenceCategory` breakdown and a headline match percentage. A **person** reads that and flips a `validated` / `invalidated` flag. There is no automatic threshold that writes the human flag; ~95% is guidance a human weighs, not a rule the system enforces.

2. **The evidence is always persisted with the flag — accountability, not prevention.** The system never blocks the human from recording a judgment. A strategy flagged `validated` at 0% agreement is *allowed* — and is saved with the full evidence snapshot (registered diagnostics, `DivergenceCategory`, backtest ID, artifact refs/hashes, flagger, timestamp) and a **required reason**. That record is proof of exactly what the match showed at flag time; the responsibility is the flagger's and is auditable forever. It is **not deployable** unless the current flag event also carries an `accepted_for_deploy` behavioral-equivalence verdict under `numerical-rigor.md`.

3. **Deployment moved menus and owns execution mode.** Deploying a validated, deployable strategy is creating a bot, so the single Deploy page re-homes from `Strategy Lab` to the `Broker` group, a sibling of `Bots` / Bot Control. Validation stays in Strategy Lab. The read-only / paper / live execution choice is a Deploy-page selector, never part of validation (validation never trades).

## Decision

### 1. Validation is a human flag over engine-match evidence — amends ADR 0020 §1

A strategy has a `validated` flag iff a **person** has reviewed the Python-vs-LEAN/QC match on the Validation page and set the flag. The "passing reconciliation ⇒ validated" rule of ADR 0020 §1 is replaced for the human flag: the reconciliation is the *evidence the human reads*, not the thing that writes the flag.

Deployability is a derived current-state projection over the append-only flag events: the latest non-superseded event must have `flag == validated` **and** `behavioral_equivalence.verdict == accepted_for_deploy`. The QC backtest ID is **provenance** (it pins which QC run is the reference), not the credential.

### 2. The Validation page refreshes engine evidence; it is an action surface — refines ADR 0020 §3–§4

The page is no longer a browse-only view of a seeded manifest. It refreshes the registered Python-vs-LEAN/QC evidence, surfaces the LEAN/QC reference, renders the match (buy/sell entry-signal alignment + PnL, `DivergenceCategory`, headline %), and records the human's flag. The Python-owned manifest projection is still the source of truth, but it now combines committed seed events with append-only runtime flag events per strategy: the flag, the authenticated flagger, the timestamp, the reason, the behavioral-equivalence verdict, and the evidence snapshot the flag was based on — *written by the human action*, not only seeded. A dedicated engine-run trigger remains a follow-up; this ADR rejects pretending a manifest refresh is a run.

### 3. Always persist the evidence and reason with the flag

Every flag write persists an immutable, append-only evidence snapshot (registered diagnostics, `DivergenceCategory` counts, backtest ID, strategy spec ref/SHA, QC audit-copy ref/SHA, and reconciliation artifact ref) plus a free-text reason. The flagger identity is derived from the server-side operator context; clients may submit the reason and selected evidence, but never `flagged_by`. If full per-trade series, input-data refs, or engine-run commits later affect deployability, their refs and hashes must be added to the snapshot before the gate can depend on them.

This event carries the "documented reason" that `.claude/rules/numerical-rigor.md` requires whenever a *behavioral* equivalence (looser than strict-float) is accepted. Save every judgment; prevent only deployability when the evidence verdict is not `accepted_for_deploy`.

### 4. Validation never trades — no broker, no orders, no gates

The Validation page places **no** orders (read-only, paper, or live), touches **no** broker, and shows **none** of the deploy readiness gates (Engine / Broker / Account / Fleet / Daemon). Its asset is fixed to the **safe canary** — the signal entity the strategy was validated on — and its sizing is a **1-share informational** readout, not an input. (The 1-share safe-canary is the backtest's single instrument, shown to explain how a position *would* be sized, not to size one.)

### 5. Execution mode (read-only / paper / live) is a Deploy-page concern — three modes plumbed, paper active, live reserved

The read-only / paper / live selector lives on the Deploy page. All three are **plumbed end-to-end** (enum, backend flag, UI), but only **read-only and paper** are built now; **live is runtime-inactive** and stays hard-blocked under the paper-only safety posture (ADR 0011). Live lights up later through a **separate Interactive Brokers live account**, gated by a future live-trading safety project. Validation "levels" are *not* execution modes: one QC backtest ID validates a strategy for all deploy modes.

### 6. The Deploy page re-homes to the Bots menu — amends ADR 0020 / PRD #917 nav

There is exactly **one** Deploy page. It moves out of `Strategy Lab` into the `Broker` group as a sibling of `Bots` / Bot Control (the route may stay `/broker/deploy`). The existing deploy form is rebuilt and re-homed, never duplicated. Validation stays in `Strategy Lab`.

### 7. Deploy signal stream defaults to the validated signal, overridable — amends ADR 0020 §2

ADR 0020 §2 said the validation-case symbol "does not default, constrain, or warn" the deployed signal stream. This is amended: the Deploy page's signal stream **defaults to the validated signal** for convenience, and remains **freely overridable** to any symbol. It still does not *constrain* (an override is valid, never an error); it now *defaults*.

## Consequences

### Positive

- The flag matches reality: a knowledgeable human accepts or rejects a strategy on the actual match evidence, instead of an arbitrary automatic threshold that writes the judgment.
- Every flag event carries an immutable, auditable answer to "how do I know this is right?" — the frozen code/data/artifact refs, match evidence, and the reason the human accepted or rejected it.
- Deployability remains tied to numerical rigor: a human judgment can be preserved for accountability without making an unproven port deployable.
- Deployment lives where the operator thinks it lives (next to the bots it creates); validation stays a pure, broker-free proving surface.
- One validation fans out to any deploy mode and any signal stream; the validated signal is a helpful default, not a cage.

### Negative / costs

- Validation is now a subjective act. Its integrity rests on the persisted evidence + reason, so those must be captured on *every* flag write — a missing reason, unauthenticated flagger, or incomplete snapshot is a hole in the audit trail.
- A guarded write path (flag + evidence + reason) and an evidence-refresh action on the Validation page are net-new (ADR 0020 assumed browse-only + a seeded manifest).
- The manifest is no longer a single mutable row; it needs committed seed events, append-only runtime event storage, and a current-state projection.
- Re-homing the Deploy page invalidates muscle memory / docs / screenshots that place it under Strategy Lab.
- Reserving `live` in the plumbing while leaving it inactive is a standing "don't wire this to a real account yet" obligation — the runtime block must stay hard (ADR 0011).

### Non-consequences

- No change to how the reconciliation itself is computed (`qc_reconciler.py`, the `reconcile-backtest` skill) — this ADR *presents* it to a human, it does not re-author it.
- No change to ADR 0021's launch-default posture or ADR 0009's sizing authority (sizing remains a deploy-time binding).
- No change to ADR 0012's signal/instrument seam.
- Does not enable live trading — `live` stays runtime-inactive and `UNSAFE`/live remains fail-closed (ADR 0011).

## Anti-patterns this ADR rejects

- An automatic pass/fail threshold (strict zero-divergence *or* a fixed %) standing in for the human judgment.
- Writing a `validated` flag without its evidence snapshot, authenticated flagger, artifact refs/hashes, and reason.
- Treating a `validated` flag with `evidence_only` or `rejected` evidence as deployable.
- Any order, broker call, or readiness gate on the Validation page.
- Treating read-only / paper / live as *validation* levels rather than *deploy* modes.
- Two deploy pages, or a deploy page left under Strategy Lab.
- Wiring `live` to a real account before the separate live-account safety project exists.

## References

- ADR 0020 (amended here), ADR 0021, ADR 0011, ADR 0009, ADR 0012.
- `CONTEXT.md` § "Strategy validation & signal stream" → "Revised 2026-07-05" block.
- `PythonDataService/app/research/parity/qc_reconciler.py` — `DivergenceCategory`.
- `.claude/rules/numerical-rigor.md` § "Equivalence levels" (behavioral level + documented-reason rule).
- `Frontend/src/app/components/strategy-validation/` (validation page), `Frontend/src/app/components/broker/broker-deploy-form/` (deploy page, re-homed), `Frontend/src/app/shell/app-sidebar.component.ts` (nav).
- PRD: `docs/architecture/strategy-validation-deploy-rehome-prd.md`.
