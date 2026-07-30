# PRD — Alpaca Clerk-Governed Bot Execution and Diagnostic Control Panel

- **Date:** 2026-07-30
- **Status:** Ready for implementation planning
- **Product surface:** Alpaca paper bots, bot details, deploy, lifecycle control, Clerk diagnostics
- **Design authority:** `docs/superpowers/specs/2026-07-30-alpaca-clerk-governed-bot-execution-design.md`
- **Builds on:** ADR 0030, ADR 0032, the broker-v2 control panel, the Alpaca Account Clerk, PR #1319's bot-trading path, the shared `ActionPlan` value types, and the existing `phase + desired_state` lifecycle model
- **First delivery slice:** Static, fixture-driven Trader and Operator example pages for the bot details surface. No broker calls, lifecycle mutations, or production contract changes in this slice.

---

## 1. Executive summary

Alpaca bot execution already has the correct durable authority: the account-scoped Alpaca Clerk. The Clerk records order intent before broker contact, serializes broker writes, resolves acknowledgement uncertainty, consumes order and fill events, reconciles the account, and can project account exposure attributable to each `strategy_instance_id`.

The remaining execution defect demonstrated by PR #1319 is duplicated truth in the strategy runtime. Its two-green-bar bot keeps a local `in_position` flag and changes it when an order is submitted rather than when the Clerk proves a fill. That local flag can disagree with the broker and disappears on restart.

This product makes the Clerk the sole execution coordinator:

- The strategy runtime emits `ENTER` and `EXIT` decisions.
- The Clerk turns those decisions into custody operations.
- The account has exposure; the Clerk attributes slices of that exposure to strategy instances.
- The bot runtime never declares itself long, short, filled, or flat.
- Operator lifecycle state remains distinct from runtime liveness and Clerk execution truth.
- STOP, EXIT, and STOP-AND-FLATTEN remain visibly different operations.
- A stopped instance with approved carryover may resume only when fresh broker evidence and the Clerk ledger prove its stopped exposure checkpoint intact.

Before changing production execution, the first slice builds static Trader and Operator example pages. Those pages serve as the visual contract for every healthy, blocked, uncertain, stopped, carryover, and exit-in-progress state in this PRD.

## 2. Problem

The current bot panel is preliminary and inherits assumptions from the earlier broker-v2 design:

- its six-station transaction rail explains individual order progress but does not adequately explain Clerk-governed instance custody;
- it can visually conflate account-net exposure with exposure attributable to one strategy instance;
- it does not show STOP, EXIT, approved carryover, Resume proof, and account freeze as distinct concepts;
- it does not provide a diagnostic target for partial entry cancellation followed by an exact close;
- its chart occupies substantial visual priority even though the principal job of this page is diagnosing whether a bot may act and whether Clerk custody is resolved;
- the current implementation can infer or duplicate execution state outside the Clerk;
- the open Stop 409 defect makes the existing control surface unreliable during active evidence updates.

The implementation needs a stable product target before its contracts are rewritten. Building static pages first lets the trader, operator, and engineering perspectives agree on information hierarchy and language without pretending that preliminary APIs are final.

## 3. Product principles

1. **One broker-write authority.** Every Alpaca order-producing path goes through the Account Clerk.
2. **Account exposure, instance attribution.** Bots do not own broker positions. The account has exposure; Clerk evidence attributes an exposure slice to a `strategy_instance_id`.
3. **No timestamp attribution.** Ownership comes from the namespace, `order_ref`, `intent_id`, and broker execution identity. Timestamps are evidence, not ownership proof.
4. **One entry-admission answer.** The Trader view receives `READY` or `BLOCKED` plus one backend-authored reason and next step.
5. **Reductions stay available.** An entry block or account freeze does not block a Clerk-proven reduction unless current exposure is itself unprovable.
6. **Desired, observed, and execution truth may disagree.** The UI renders all three rather than compressing them into one status.
7. **EXIT means attributed-flat.** A successful EXIT receipt proves no working entry/exit remains and the instance-attributed exposure slice is zero.
8. **STOP does not silently flatten.** STOP prevents future strategy work and cancels working entries. It may preserve exposure only under approved carryover.
9. **Resume is proof, not optimism.** A stopped instance resumes with carryover only if the Clerk proves the stopped exposure checkpoint intact.
10. **Backend authors meaning.** Angular renders backend-authored labels, explanations, states, and next steps. It never derives exposure or safety verdicts.
11. **Static examples precede production wiring.** The first slice is an explicit future-development artifact and interaction target, not a mock backend disguised as production.

## 4. Users and primary jobs

### Trader

The trader needs to answer:

- Is this bot allowed to enter a trade now?
- What is it trying to do?
- What account exposure is attributed to it?
- Is an entry or exit still working?
- What happened in plain trading language?
- What is the one safe action available now?

The Trader lens intentionally omits most internal machinery.

### Operator

The operator needs to answer:

- Which authority currently blocks progress?
- Is the problem instance-scoped, runtime-scoped, or account-wide?
- What did the Clerk accept, submit, observe, cancel, fill, or fail to prove?
- Is the account state unattributable or unprovable?
- Can the instance safely Resume with its stopped exposure?
- Is EXIT still cancelling, closing, verifying, or terminal?
- Which exact receipt and broker evidence support the displayed state?

The Operator lens exposes the real seams while keeping the vocabulary closed and small.

## 5. Canonical product language

### Strategy instance

A lifetime configured instance of a strategy, identified by `strategy_instance_id`. One instance may have many process runs, but its immutable configuration, Clerk namespace, custody history, and attribution remain continuous.

### Run

One runtime incarnation, identified by `run_id`. A new run never creates a new exposure owner and never breaks Clerk custody continuity.

### Account exposure

The broker-observed net position of the connected Alpaca account.

### Instance-attributed account exposure

The Clerk projection of the portion of account exposure supported by orders and fills carrying one strategy instance's exact namespace. It is not a broker-native sub-position.

### Clerk effect operation

One idempotent response to an actionable strategy or operator decision. An effect operation may create zero, one, or multiple child cancel/submit intents.

### Clean EXIT

A terminal Clerk effect proving that working entry and exit orders for the operation are terminal and the instance-attributed account exposure is zero.

### Carryover stop checkpoint

The durable Clerk-backed evidence captured when STOP is allowed to leave approved instance-attributed account exposure in place.

### Resume custody proof

A fresh comparison between the carryover stop checkpoint, current Clerk attribution, immutable strategy configuration, and broker account truth. Resume is allowed only on an exact proof.

## 6. Authority model

| Concern | Canonical author | What it may claim |
|---|---|---|
| Strategy decisions | Strategy runtime using the canonical strategy kernel | `ENTER`, `EXIT`, quiet evaluation, strategy error |
| Desired lifecycle state | Lifecycle manager | `RUNNING`, `PAUSED`, `STOPPED` |
| Observed duty/runtime state | Lifecycle manager/supervisor | `ON_DUTY`, `OFF_DUTY`, `RETIRED`, run outcome |
| Broker intent and order lifecycle | Alpaca Account Clerk | accepted custody, submitted, acknowledged, rejected, uncertain, recovered, cancelled, filled |
| Account exposure attribution | Alpaca Account Clerk | signed quantity per strategy namespace/instrument |
| Account reconciliation/freeze | Alpaca Account Clerk | attributable, unattributable, provable, unprovable |
| Trader/operator prose | Python projection services | headline, explanation, next step, action availability |
| Presentation | Angular | layout, formatting, interaction with closed server-presented actions |

No new Execution Coordinator service is introduced.

## 7. Strategy and Clerk operations

### 7.1 ENTER

The strategy emits an `ENTER` decision. The Clerk:

1. validates the active instance/run binding;
2. deduplicates the restart-stable `decision_id`;
3. verifies entry admission;
4. reads the deploy-time `ActionPlan`;
5. ensures there is no attributed exposure or working entry for the instance;
6. records custody before broker contact;
7. derives BUY/SELL from the configured long/short leg;
8. submits through Alpaca and resolves the lifecycle.

A repeat ENTER while exposure or an entry operation exists is an idempotent no-op. Scaling is out of scope.

### 7.2 EXIT

EXIT is one Clerk effect operation, not one broker order:

1. block further ENTER operations for the instance;
2. locate working entry orders;
3. request their cancellation;
4. wait for cancellation/fill outcomes to become terminal;
5. recompute the instance-attributed signed quantity;
6. if non-zero, submit the exact reducing order;
7. consume partial fills and recover uncertain outcomes;
8. reconcile until the attributed quantity is zero or the outcome is honestly unprovable;
9. emit one terminal effect receipt.

An EXIT decision may therefore map to several child `intent_id`/`order_ref` values. `decision_id` deduplicates the effect operation, not an individual order.

The runtime stays active after strategy EXIT and may later produce another ENTER.

### 7.3 STOP

STOP is an operator lifecycle operation:

1. persist desired state `STOPPED`;
2. prevent new strategy evaluations and ENTER decisions;
3. cancel working entry orders;
4. allow already-custodied exit work to resolve;
5. stop the runtime;
6. inspect final instance-attributed account exposure.

Outcomes:

- No attributed exposure: `STOPPED_FLAT`.
- Attributed exposure with approved carryover: persist a carryover stop checkpoint and return `STOPPED_WITH_APPROVED_ATTRIBUTED_EXPOSURE`.
- Attributed exposure without approved carryover: return `STOP_REQUIRES_FLATTEN`; never claim a clean stop.

STOP never silently becomes STOP-AND-FLATTEN.

### 7.4 STOP-AND-FLATTEN

STOP-AND-FLATTEN combines STOP with the Clerk EXIT operation. It completes only when:

- the runtime is stopped;
- working entry orders are terminal;
- closing orders are terminal;
- the instance-attributed account exposure is zero.

Terminal receipt: `STOPPED_AND_ATTRIBUTED_FLAT`.

### 7.5 FLATTEN after STOP

A stopped runtime is not required for Clerk custody. A stopped instance with approved carryover may later request FLATTEN directly through the Clerk. The operation does not require a live strategy process.

### 7.6 RESUME with carryover

Resume creates a new `run_id` for the same `strategy_instance_id`. It is admitted only when a fresh Resume custody proof establishes:

- the instance is active and not retired;
- desired state is durably `STOPPED`;
- the strategy and ActionPlan hashes match the stop checkpoint;
- no unresolved or uncertain instance intent exists;
- no working entry or exit remains;
- current instance-attributed exposure exactly matches the checkpoint by instrument and signed quantity;
- the account-net broker snapshot is fully explainable by Clerk-attributed instance and manual slices;
- no foreign or unprovable account mutation exists.

Price changes do not invalidate Resume. Quantity, direction, instrument identity, configuration, or custody uncertainty do.

The system never automatically resizes, adopts mismatched exposure, or flattens during Resume.

## 8. Admission and diagnostic scopes

The product avoids a long list of independent gates. It exposes one entry-admission result and diagnostic facts grouped by authority.

### Entry admission

```text
READY
BLOCKED(reason_code, explanation, next_step)
```

`READY` requires:

- the connected Alpaca paper account is attached and approved;
- startup reconciliation completed;
- no account-wide freeze exists;
- execution state remains observable;
- the instance/run binding is active;
- the instance has no unresolved entry-affecting uncertainty;
- the strategy has a fresh, valid market-data input.

### Instance-scoped blocks

- uncertain submit for this instance;
- active entry/exit effect already in progress;
- crash-loop/restart-intensity block for this instance;
- carryover checkpoint mismatch;
- immutable configuration mismatch;
- retired identity.

These do not freeze healthy siblings.

### Runtime/shared-feed blocks

- no fresh market-data bar;
- strategy runtime unavailable;
- strategy evaluation error.

These do not create an account freeze.

### Account-wide freezes

Only the Clerk issues a durable account freeze:

1. `ACCOUNT_STATE_UNATTRIBUTABLE` — broker state exists but cannot be mapped to exact Clerk custody.
2. `ACCOUNT_STATE_UNPROVABLE` — the Clerk cannot establish current order/exposure truth from durable custody plus fresh broker observation.

Known Clerk-owned manual activity remains attributed and is displayed separately. It is not foreign activity.

An automatically recovering condition is a transient block, not a durable freeze.

## 9. First delivery slice — static diagnostic example pages

### 9.1 Purpose

Create fixture-driven static pages that demonstrate the target bot details experience before production APIs and actions are changed.

This slice answers:

- Is the Trader lens understandable without internal architecture knowledge?
- Can the Operator lens pinpoint the actual blocking authority?
- Are STOP, EXIT, STOP-AND-FLATTEN, carryover, and Resume visually distinct?
- Can all planned states render without Angular inventing meaning?
- Is the chart supporting diagnosis rather than dominating it?

### 9.2 Route and containment

Add a development/example route under the existing application shell:

```text
/examples/alpaca-bot-control
```

The route:

- is marked clearly as **Static product example — no broker actions**;
- uses typed local fixtures only;
- performs no HTTP calls;
- cannot submit lifecycle or broker mutations;
- is excluded from normal Broker navigation until the examples are approved;
- renders both Trader and Operator lenses using the same fixture contract;
- includes a scenario selector for design review.

The implementation plan may choose a dev-only route, a fixture gallery inside the app, or the repository's closest established component-example precedent. It must not invent a production-looking API fallback.

### 9.3 Visual direction

The page is a diagnostic instrument, not a generic financial dashboard.

- **Visual thesis:** custody and proof, not decorative market data.
- **Signature element:** a Clerk custody spine connecting strategy decision, custody acceptance, broker effects, attributed exposure, and reconciliation.
- **Palette roles:** account truth/neutral ink, verified teal, active/waiting blue, caution amber, blocked red, and quiet evidence grey. Production code uses existing theme tokens rather than hard-coded colors.
- **Typography:** existing application/PrimeNG typography; tabular numerals for quantities, prices, times, and receipt sequences; monospace only for opaque evidence IDs.
- **Motion:** none required for static examples. Production may use one restrained transition when an operation advances; reduced-motion remains authoritative.
- **Charts:** one compact diagnostic chart or price strip with attributed fill markers. No attempt to reproduce the IBKR dual-chart layout when it does not help answer the Alpaca custody question.

### 9.4 Trader lens

Single job: tell the trader whether this strategy instance may act, what account exposure is attributed to it, and the one safe action available.

```text
┌ SPY · Two-green-bar · Long · Alpaca paper ───── [ READY ] ┐
│ Desired     RUNNING                                         │
│ Runtime     On duty · last bar 10:42:00 NY                  │
│ Execution   Account exposure attributed here: FLAT          │
│                                                            │
│ Watching for two consecutive green one-minute bars.         │
│                                                            │
│ [ compact price context with attributed fill markers ]      │
│                                                            │
│ Latest trade / active operation / honest empty state        │
│                                                [ Stop ]      │
└────────────────────────────────────────────────────────────┘
```

Requirements:

- entry-admission chip with backend-authored reason when blocked;
- three truth rows: Desired, Runtime, Execution;
- phrase exposure as account exposure attributed to the instance;
- one primary action;
- operation summary when ENTER, EXIT, STOP, or Resume proof is active;
- compact price context;
- realized/open P&L only when supported by canonical attributed fills;
- no raw gates, hashes, journal sequence, or broker payloads;
- no green “safe” claim when evidence is stale or unknown.

### 9.5 Operator lens

Single job: identify the blocking authority and show the exact evidence chain.

```text
┌ Instance scope ───────────────┬ Account / Clerk scope ────────────────┐
│ Desired: STOPPED             │ Account: Alpaca paper                 │
│ Runtime: OFF_DUTY            │ Freeze: none                          │
│ Active run: none             │ Reconciliation: proven · 4s ago       │
│ Carryover: approved          │ Execution observation: healthy        │
├──────────────────────────────┴───────────────────────────────────────┤
│ CLERK CUSTODY SPINE                                                  │
│ Decision → Custody → Cancel entries → Close exposure → Verify zero   │
│    ✓          ✓             ✓              waiting          waiting  │
├───────────────────────────────────┬──────────────────────────────────┤
│ Attributed account exposure       │ Resume custody proof             │
│ SPY +10 · checkpoint +10          │ Config ✓ Quantity ✓ Orders ✓     │
│ Working entries 0 · exits 1       │ Result: not available during EXIT│
├───────────────────────────────────┴──────────────────────────────────┤
│ Evidence timeline / receipts / exact opaque identifiers              │
└─────────────────────────────────────────────────────────────────────┘
```

Requirements:

- clearly separate instance, runtime/feed, and account/Clerk scopes;
- show the custody spine for the selected effect operation;
- show entry admission separately from reduction availability;
- show attributed exposure buckets and known manual slices;
- show Resume checkpoint comparison field-by-field;
- show freeze category, proof age, and Clerk-authored next step;
- show a bounded timeline with expandable evidence;
- raw backend codes use `receiptLabel`; opaque IDs remain verbatim;
- action controls are static disabled examples in Slice 1.

### 9.6 Static scenario catalog

The example route must render at least these fixtures:

| Scenario | Trader lesson | Operator diagnosis |
|---|---|---|
| `running_ready_flat` | Ready to enter | All entry-admission facts proven |
| `running_attributed_long` | Running with SPY +10 attributed | Clerk fill fold and account-net decomposition agree |
| `entry_partial_pending` | Entry pending 4/10 | Working remainder and partial fill shown separately |
| `exit_cancelling_entry` | Exit in progress | Cancel must become terminal before exact close |
| `exit_closing` | Closing attributed exposure | Reducing order partial-fill progress |
| `exit_complete` | Exit complete and attributed-flat | Terminal clean-exit receipt |
| `stopped_flat` | Stopped, no exposure | Resume permitted after ordinary lifecycle checks |
| `stop_requires_flatten` | Stop cannot complete cleanly | Carryover forbidden with attributed exposure |
| `stopped_carryover_intact` | Stopped with approved SPY +10 | Resume custody proof passes |
| `stopped_carryover_mismatch` | Resume blocked | Broker/Clerk quantity or configuration differs from checkpoint |
| `instance_submit_uncertain` | This bot blocked | Sibling instances remain unaffected |
| `market_data_stale` | Waiting for fresh market data | Runtime block; account remains attributable |
| `account_unattributable` | Account trading frozen | Foreign/unmapped broker state |
| `account_unprovable` | Account trading frozen | Current account truth cannot be established |
| `known_manual_exposure` | Bot remains honestly scoped | Manual Clerk exposure shown as a separate attributed slice |

Every scenario needs a one-sentence “why this exists” note in the selector.

### 9.7 Static-slice acceptance criteria

- Both lenses render for every scenario without network calls.
- The scenario selector identifies the active diagnostic condition.
- Trader copy contains no raw reason code or architecture term without a readable label.
- Operator copy preserves exact evidence identifiers where shown.
- No fixture claims that a bot owns broker exposure.
- STOP, EXIT, and STOP-AND-FLATTEN never share the same label or completion receipt.
- `stopped_carryover_intact` visibly permits Resume; the mismatch fixture blocks it.
- Instance uncertainty does not render as an account freeze.
- Market-data loss does not render as an account freeze.
- Known manual activity does not render as foreign.
- EXIT progress shows cancel, close, and verify as separate stages.
- Backend-authored labels are represented directly in fixture contracts; components do not derive safety prose.
- Keyboard navigation, visible focus, icon-plus-text state communication, and AXE/WCAG-AA checks are included.
- Desktop is designed for 1440×900; narrow layouts stack without losing scope labels.
- No new frontend dependency is introduced.

## 10. Production slices after static approval

### Slice A — Clerk effect operations

- Add restart-stable `decision_id` and Clerk effect-operation identity.
- Add `execute_for_instance(sid, run_id, decision_id, purpose)`.
- Enforce one stock entry leg and one corresponding close leg for Alpaca v1.
- Make Clerk custody cancellation-resistant once durably accepted.
- Implement ENTER and EXIT operation folds with child intents.
- Add backend-authored terminal effect receipts.
- Preserve risk-reducing EXIT/FLATTEN availability during entry blocks when exposure is provable.

### Slice B — canonical strategy runtime

- Extract/reuse the canonical deployment-validation signal kernel.
- Delete bot-local execution truth.
- Feed Clerk-authored fills and operation outcomes into strategy evaluation.
- Reconstruct signal state from canonical bars and Clerk fill evidence.
- Add golden parity and restart tests.

### Slice C — lifecycle, carryover, Resume, and action concurrency

- Implement STOP, STOP-AND-FLATTEN, and stopped-instance FLATTEN.
- Add account-level carryover permission plus explicit per-deployment opt-in.
- Persist carryover stop checkpoints as references/projections over Clerk authority.
- Implement fresh Resume custody proof.
- Replace the full-panel Stop revision with an action-specific control revision.
- Make action idempotency durable and prevent duplicate execution after an in-flight timeout.

### Slice D — production diagnostic contracts

- Add backend-authored Trader and Operator view contracts based on the approved static fixture shape.
- Make the backend author execution summary labels and next steps.
- Add bounded operation/evidence timelines.
- Preserve raw-ID and `receiptLabel` rules.
- Replace static fixtures with production projections without changing component meaning.

### Slice E — Alpaca paper deploy

- Ship a custom Alpaca paper deploy page.
- Remove observation/read-only mode from this order-producing workflow.
- Select only supported validated strategies.
- Support one STK ActionPlan entry leg in v1.
- Keep option-capable shared value types dormant until Alpaca option execution is implemented.

### Slice V — paper validation

- Run fault-injection tests for rejection, uncertainty, cancellation races, partial fills, restart, and stopped carryover.
- Run the five-always-running churn drill.
- Run the eight-bot paper exercise through panel actions only.
- Require no direct runner-route wind-down deviation.
- Publish updated audit evidence.

## 11. Functional requirements

1. Every broker order attributable to an Alpaca strategy instance is accepted through the Clerk.
2. The strategy runtime cannot submit raw BUY/SELL sides.
3. The strategy runtime cannot author execution exposure.
4. A Clerk-accepted effect survives caller cancellation and runtime exit.
5. EXIT is idempotent by `decision_id`.
6. EXIT completes only with an attributed-flat proof or an honest non-terminal/unprovable outcome.
7. STOP cancels working entries and never silently flattens.
8. STOP with non-zero attributed exposure requires approved carryover or explicit flattening.
9. Resume with carryover requires an exact fresh proof.
10. Account freezes are limited to unattributable or unprovable account state.
11. Instance/runtime blocks cannot freeze healthy siblings.
12. Known Clerk-owned manual activity remains separately attributable.
13. The frontend never derives exposure, readiness, freeze, or Resume eligibility.
14. All wire/storage timestamps are `int64 ms UTC`.

## 12. Non-functional requirements

- Clerk journal remains the sole execution store.
- No database is required to establish canonical execution truth; a rebuildable projection may be added for read performance.
- All identity fields are bounded and validated.
- Every bug fix includes a regression test.
- Paper-only execution is enforced at the submission boundary.
- No silent exception handling or print/debug output.
- Panel projection does not perform full journal scans per poll at production scale.
- Polling or later streaming must serialize refreshes and prevent stale-route writes.

## 13. Non-goals

- Real-money Alpaca trading.
- Scaling into or out of positions.
- Multi-leg or option execution in v1.
- Shared external/manual Alpaca account writers outside the Clerk.
- A second shadow execution ledger.
- IBKR behavior changes.
- A generic multi-broker execution abstraction.
- Implementing the static example pages in this documentation PR.

## 14. Success measures

- Traders correctly distinguish STOP from STOP-AND-FLATTEN in moderated review.
- Operators identify the blocking scope and next step from each static scenario without reading raw JSON.
- No production order path bypasses Clerk custody.
- Rejected or uncertain orders never advance attributed exposure incorrectly.
- Resume never adopts a changed position or configuration.
- Same-symbol sibling positions remain correctly attributed.
- The implementation audit completes without an unexplained account freeze or direct runner-route deviation.

## 15. Delivery and issue strategy

This PRD remains one product document. After the static examples are approved, convert the production slices into independently grabbable issues with explicit dependencies:

```text
Static examples
  → Clerk effect operation
  → canonical strategy runtime
  → lifecycle/carryover/Resume
  → production view contracts
  → deploy
  → paper validation
```

Do not parallelize changes that compete for Clerk journal or lifecycle authority until their contracts are merged.

## 16. Definition of Done for this PRD

- The Clerk-governed design spec and glossary use the same exposure and lifecycle language.
- The first implementation slice is explicitly static and fixture-driven.
- Every planned blocking scope has a named static scenario.
- EXIT, STOP, STOP-AND-FLATTEN, carryover, and Resume semantics are closed.
- The future production slices and validation gates are recorded.
- The documentation is published through a dedicated GitHub pull request.
