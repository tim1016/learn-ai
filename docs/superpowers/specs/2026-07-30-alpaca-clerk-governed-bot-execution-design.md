# Alpaca Clerk-Governed Bot Execution — Design

**Date:** 2026-07-30
**Status:** Approved design, pre-implementation
**Supersedes framing of:** the codex "Strategy Instances Governed by an Account-Rooted Alpaca Clerk" proposal (7-slice greenfield). This design is the *evaluated, simplified* version of that proposal.
**Product requirements:** `docs/prds/alpaca-clerk-governed-bot-control.md`

---

## 1. Objective

Make the **Alpaca Account Clerk** the single broker-write authority and execution coordinator for Alpaca bots, and delete every competing source of execution truth. A bot is a lifetime strategy instance (`strategy_instance_id`); its process may start, stop, crash, and resume, but the Clerk continuously owns order custody, attributed exposure, reconciliation, and transaction history.

There is **no separate execution coordinator, risk service, or submission gate** — none was ever built, and none will be. The Clerk *is* the coordinator.

## 2. The reframe (why this is small)

The codex proposal reads as greenfield, but the durable foundation is already shipped and PR #1319 demonstrates a bot path over it. Ground truth in the base branch plus the PR #1319 patch:

- PR #1319's bots submit through the Clerk: `app/broker/alpaca/clerk/clerk.py` → `submit_for_instance(strategy_instance_id, legs)`. No coordinator sits between bot and Clerk.
- The Clerk already owns durable intents, `intent_id` identity, single-writer broker submission, ack/reject/uncertain, reconciliation sweep verdicts, holds, and **instance-attributed account exposure** (`app/broker/alpaca/clerk/exposure.py`: `project_instance_exposure(namespace=…)`, `project_instance_timeline()`, `verify_flatten()`).
- Broker ownership already keys on lifetime `strategy_instance_id` through namespace `learn-ai/{sid}/v1`; `run_id` remains per-incarnation origin metadata and must be added to the Alpaca custody receipt where needed.

**The principal duplicated authority in PR #1319** is bot-local execution truth: `app/services/bot_trade_strategy.py` holds `in_position: bool`, sets it `True` on submit before any ack/fill (assumes immediate fill — documented divergence #2), resets it daily, and loses it on restart. Deleting it removes the competing execution state. The implementation still adds effect-operation identity, restart-stable decision idempotency, cancellation-resistant Clerk custody, lifecycle action corrections, and production projections.

## 3. Authority boundaries

- **Strategy runtime (thin):** consumes bars through the canonical strategy kernel, maintains indicator/signal state, and emits a binary decision — **ENTER** or **EXIT** — tagged with `run_id` + restart-stable `decision_id`. It authors **no** execution truth: it never sets `in_position`, never declares a fill, never declares long/short/flat.
- **Alpaca Account Clerk (sole authority):** admission, durable effect operation and child intents, idempotency, side derivation, broker submission, ack/reject/uncertain/recovery, cancellation, partial/complete fills, instance-attributed account exposure, reconciliation, and account-level freeze. Stays alive while the account is connected, even with zero bots running.
- **Lifecycle manager:** durable operator intent (`desired_state.json`), start/stop processes, assign `run_id`, report observed process state. Submits no orders.
- **Unified bot view:** a projection, not an authority. Joins identity + desired + observed + Clerk execution + activity by the available identity chain. It never infers exposure and never persists a second execution ledger. Python authors summary labels and next steps; Angular renders them.

## 4. The intent contract (keystone — Slice A)

**Reuse the existing IBKR effect-classification pattern**, but do not claim wire-contract identity where it does not exist:

- `app/engine/live/account_effect_models.py` already models exact close/cancel requests and classifies account effects, including `ENTRY`; Alpaca adds its own high-level `ENTER | EXIT` effect-operation contract.
- The clerk derives order side from signed quantity; the caller never authors BUY/SELL (`live_portfolio.py:1669`: `action = "BUY" if qty > 0 else "SELL"`).
- Direction `LONG(1)/FLAT(0)/SHORT(-1)`; instrument `sec_type: STK | OPT`; long/short + stock/option are properties of the **deploy-time leg**, not the per-decision intent.

**Alpaca runtime → Clerk call:**

```
execute_for_instance(
    strategy_instance_id,
    run_id,
    decision_id,
    purpose,          # ENTER | EXIT
)
```

The deploy-time **ActionPlan** carries the configured legs (`position: long|short`, `instrument: {kind: stock|option}`, `qty_ratio`). Long/short is fixed at deploy, like IBKR asset-position selection. Alpaca v1 enforces exactly one STK `on_enter` leg and one matching `close_leg`; the shared value types remain option-ready without enabling option execution. The Clerk resolves the concrete effect:

- **ENTER:** read attributed exposure. If flat, submit the configured `on_enter` leg (side derived from the leg's `position`). If already in the configured position, **no-op** (scaling is out of scope; a repeat ENTER cannot add size).
- **EXIT:** bring the instance-attributed account exposure to zero. Freeze further ENTER effects for the instance; cancel working entry orders; wait for cancellation/fill outcomes to become terminal; recompute the signed attributed quantity; submit the exact reducing leg; consume partial fills; and reconcile until the attributed quantity is zero or the outcome is honestly unprovable. Existing `verify_flatten()` is the pre-submit custody check, not the terminal zero proof. This correctly handles an entry filled 4/10: cancel the remaining 6, wait for the terminal cancellation/fill fact, then reduce the final filled quantity.

The runtime never needs to know its position — that is what makes deleting `in_position` possible.

**Identity and idempotency:** `decision_id` is the effect-operation boundary and must remain stable across a new `run_id` replay of the same actionable bar. One decision maps to one Clerk effect operation, which may map to zero, one, or several child `intent_id` / `order_ref` values. The Clerk dedups the effect operation inside the intake lock; the exposure check is belt-and-suspenders for repeat ENTER.

**Custody transfer:** once the Clerk has durably accepted an effect operation, cancellation of the strategy task cannot cancel Clerk resolution. The Clerk owns the operation through a terminal receipt or an honest unprovable state.

## 5. Single admission verdict (Slice B)

The caller sees **one entry-admission result**, not a chain of gateways:

```
READY  |  BLOCKED(reason_code)
```

`READY` internally means: account attached + startup reconciliation done; no active account freeze; execution state observable; the instance/run binding is active; no entry-affecting instance uncertainty; and fresh strategy input. Instance-specific custody is checked as part of the command, not exposed as a second gateway. Granular `reason_code` is preserved *behind* the verdict for operator display — this is a presentation/API consolidation, **not** the removal of any safety check.

**Purpose-sensitive admission:** an entry block or account freeze blocks ENTER. A Clerk-proven reduction remains available unless current attributed exposure is itself unprovable.

**Scope of a block:** a single instance's `submit_uncertain`, crash loop, active effect, configuration mismatch, or carryover mismatch blocks **only that instance**. Stale market data blocks strategy evaluation rather than freezing the account. Only genuinely account-level conditions freeze the account (§6).

## 6. Account-wide freeze — Clerk-only, enumerated

The Clerk is the **only** issuer of an account-wide freeze, and exposes two durable categories:

| Condition | Trigger | Clears |
|---|---|---|
| **ACCOUNT_STATE_UNATTRIBUTABLE** | Broker state exists but cannot be mapped to exact Clerk custody | Fresh reconciliation plus recovery proof or audited operator resolution |
| **ACCOUNT_STATE_UNPROVABLE** | The Clerk cannot establish current order/exposure truth from durable custody plus fresh broker observation | Fresh proof that restores account truth, or audited operator resolution |

Known Clerk-owned manual activity is a separate attributed slice, not foreign activity. Crash loops and instance-owned uncertainty remain instance-scoped. Market-data loss is a runtime/shared-feed block. Execution observation loss becomes `ACCOUNT_STATE_UNPROVABLE` only when the Clerk cannot compensate with fresh broker reconciliation. A condition that auto-clears is a transient block, not a durable freeze.

## 7. Lifecycle (Slice B)

Reuse the existing model — **do not** invent new enums. `phase (OFF_DUTY | ON_DUTY | RETIRED)` + `desired_state.json (RUNNING | PAUSED | STOPPED)` already *is* the desired-vs-observed separation. Verbs:

- **Pause:** process alive, no new decisions. (`desired_state: PAUSED`, already works.)
- **Stop:** persist `STOPPED`, prevent new decisions, cancel working entries, allow already-custodied exits to resolve, then stop the runtime. With zero attributed exposure it completes `STOPPED_FLAT`. With non-zero exposure it either records an approved carryover checkpoint or returns `STOP_REQUIRES_FLATTEN`. Stop never silently flattens.
- **StopAndFlatten:** stop new decisions + resolve/cancel pending entries + close instance-attributed account exposure + prove attributed zero. It is a Clerk-owned operation and does not require a live strategy process.
- **Retire:** terminal `phase: RETIRED`; no future runs.

Honesty requirement: `Desired: STOPPED / Runtime: EXITED / Execution: LONG 10 SPY` is a **legal, must-render** state. The card shows all three rows and offers the one resolving action (Flatten & verify).

**Carryover and Resume:** account policy determines whether carryover may be approved; each deployment must opt in. A carryover STOP records a Clerk-backed checkpoint after working orders settle. Resume creates a new `run_id` for the same `strategy_instance_id` only when fresh broker truth and the Clerk projection exactly match the checkpoint by immutable configuration, instrument, direction, and signed quantity. Price changes are irrelevant. A mismatch, unresolved intent, changed configuration, foreign state, or unprovable account blocks Resume without resizing, adoption, flattening, or other automatic mutation.

**Defect #10:** the Stop 409 is an action-revision defect, not an exposure semantic. Slice B must replace the full-panel revision with an action-specific control revision and make action idempotency durable.

## 8. Evidence model — two ledgers, one join

- **Clerk journal** = canonical for all execution facts (intent, submitting, acked, rejected, uncertain, recovered, cancelled, partial/complete fills, reconciliation, attributed exposure). One store — the existing order journal. No second execution ledger.
- **Strategy activity** = canonical for evaluated bars, actionable decisions, strategy errors. Quiet HOLD evaluations fold into a heartbeat/cursor — **no append-only receipt per bar.**
- **Unified projection** joins the two through the identity chain. It may copy facts into a response but persists no second execution authority.

Durable strategy state on restart: **none required** for the 2-green-bar strategy if exact state is reconstructed from canonical bars plus Clerk fill evidence and guarded by a restart-stable `decision_id`. The live runtime must reuse/extract the canonical deployment-validation decision kernel and prove parity; it may not retain a second hand-coded strategy. Principle: *Clerk owns "what account exposure is attributed here"; the runtime recomputes "where am I in my signal logic."*

## 9. The Alpaca deploy page (Slice C)

A **custom, independent** rebuild modeled on IBKR's `/broker/deploy` (`BrokerDeployFormComponent`), minus the ceremony. The IBKR "launch statement" is its right-side "Launch strategy" review panel with identity/exposure **coherence confirmation cards** + QC-parity ceremony — the weight Alpaca doesn't need.

- **Keep (left form):** deployment name · supported validated strategy select · symbol · sizing preset (`safe_canary` = 1 share/signal default, or custom) · one STK **ActionPlan** leg (long/short, qty ratio) · explicit Alpaca paper account · daily order limit.
- **Drop:** coherence cards, QC audit/backtest-parity fields, IBKR reconnect framing. Deploy = **select + confirm**, one POST, no coherence-recovery loop.
- **Execution posture (locked):** this is an Alpaca paper-order workflow. There is no `read_only` capability radio; observation-only validation remains outside this execution surface so it does not require a shadow exposure authority.
- **Instrument scope (locked):** stocks-first, options-ready. The leg model/types carry the full stock+option shape; the first shippable path enforces STK-only (matches the current Alpaca clerk phase). Options light up later with no schema change.
- **Reuse (locked):** build a **separate** Alpaca deploy component + Alpaca intent path (custom to Alpaca), but **share the pure, broker-agnostic ActionPlan leg value-types** (avoids canonical-model duplication). Nothing else is shared with IBKR.

## 10. The unified bot view (Slice C)

Model on the existing trader-first Verdict Card (`bot-control/verdict-card`). Three rows that are allowed to disagree, one admission chip, one primary verb (Button Rule):

```
┌─ SPY · two-green-bar · long · stock · Paper ──── [ READY ]
│  Intent      Desired: RUNNING          (operator wish)
│  Runtime     Process: on duty          (observed liveness)
│  Execution   FLAT                       (Clerk-authored, attributed exposure)
│  [ End day ]   ·   Stop & flatten   ·   Retire & replace
└─
```

- Admission chip: `READY | BLOCKED(reason)`, granular reason in trader language behind it.
- The Python projection derives the execution label from attributed account exposure + working intents: `FLAT`, `LONG 10 SPY`, `ENTRY PENDING (4/10)`, `EXIT PENDING`. Angular renders it.
- Event stream = joined activity+custody timeline, HOLD bars folded to a heartbeat cursor.
- Frontend **renders, never infers**; raw backend codes render through the `receiptLabel` pipe.

## 11. Disposition of PR #1319 (the Alpaca 8-bot path)

- **Keep:** the Alpaca Clerk (`clerk.py`, journal, `exposure.py` projections, holds, stream-health gate), the `learn-ai/{sid}/v1` namespace, `submit_for_instance`, and the 8-bot evidence harness.
- **Rewrite:** `bot_trade_strategy.py` into a thin adapter over the canonical deployment-validation decision kernel; delete `in_position`, stop assuming immediate fill, and change submission from raw `BrokerOrderLeg`s to Clerk `ENTER/EXIT` effect operations.
- **Delete:** bot-local execution truth, duplicate live strategy math, and any account-exposure attribution outside the Clerk.

The authority simplification deletes competing truth, but the effect-operation, carryover/Resume, control revision, projection, and diagnostic UI work remain substantive.

## 12. Scope decisions (locked)

1. **All slices** form one coherent product design; implementation remains independently reviewable.
2. **Intent shape:** binary ENTER/EXIT, long/short at deploy, Clerk derives side (§4).
3. **Effect shape:** one decision → one Clerk effect operation → zero-to-many child broker intents.
4. **Uncertain block:** instance-scoped by default; account freeze only when account state becomes unattributable or unprovable.
5. **Reduction availability:** proven EXIT/FLATTEN stays available during entry blocks.
6. **Config:** immutable per `strategy_instance_id`; a material change = a new instance. No config-versioning (YAGNI).
7. **Account exclusivity:** the Alpaca account is Clerk-exclusive; foreign activity → account freeze. Known Clerk manual activity remains attributable.
8. **Carryover:** account policy enables the capability; each deployment opts in; Resume requires exact fresh checkpoint proof.
9. **Execution posture:** Alpaca paper orders only; no read-only shadow mode.
10. **Instruments:** exactly one STK entry/close pair in v1; shared types remain options-ready.
11. **Reuse:** custom Alpaca page + intent path; share only pure ActionPlan leg types.

## 13. Non-goals

- Scaling in/out or target-position intents (belongs to the strategy layer, not the control panel).
- Options execution in the first pass (model-ready, enforcement-deferred).
- Multi-writer / externally-shared Alpaca account reconciliation.
- Observation/read-only execution with simulated exposure.
- New lifecycle enums (reuse `phase` + `desired_state`).
- Any IBKR behavior change — this is Alpaca-only.

## 14. Slices & Definition of Done

- **Slice S — static diagnostic examples:** fixture-driven Trader and Operator example pages for every admission, EXIT, STOP, carryover/Resume, runtime block, and account-freeze scenario. DoD: no HTTP/mutation path; visual contract approved.
- **Slice 0 — ADR + glossary:** amend the Account Clerk ADR: Clerk as execution coordinator; ENTER/EXIT effect operation; two account-freeze categories; desired-vs-observed-vs-execution; Stop vs StopAndFlatten; carryover/Resume proof; Clerk-exclusive account. DoD: ADR merged, glossary updated.
- **Slice A — keystone:** cancellation-resistant `execute_for_instance(sid, run_id, decision_id, purpose)`; effect operation + child intents; Clerk derives leg from attributed account exposure; restart-stable decision dedup; canonical strategy kernel; delete `in_position`. DoD: bot never authors a side or execution truth; restart preserves custody; §15 tests 1–9 pass.
- **Slice B — one verdict + honest lifecycle:** purpose-sensitive `READY | BLOCKED(reason_code)` entry admission; instance-scoped uncertainty; §6 account-freeze set; Stop/StopAndFlatten/carryover/Resume; action-specific revision and durable action idempotency. DoD: §15 tests 10–16 pass; defect #10 regression test green.
- **Slice C — deploy page + production bot view:** custom paper-only Alpaca deploy page (§9); backend-authored unified 3-row bot view (§10), replacing approved static fixtures without semantic drift. DoD: deploy = select+confirm; STOPPED/EXITED/LONG and Resume-proof cases render honestly; frontend infers no exposure.
- **Slice V — validation:** §15 executed against a paper account.

## 15. Validation checklist (operational)

1. Rejected entry leaves the instance flat.
2. Uncertain entry prevents a second entry **on that instance only** (siblings unaffected).
3. Rejected exit leaves exposure open.
4. Partial fills produce correct attributed exposure (`ENTRY PENDING (n/m)`).
5. Two instances trade the same symbol; instance-attributed account exposure stays distinct.
6. Broker-net flat does **not** imply each instance is flat.
7. A stopped instance's attributed account exposure remains Clerk-custodied and renders honestly.
8. Clerk resolves a working order after the runtime crashes.
9. Retrying a decision across a new `run_id` does not duplicate an effect operation or broker order.
10. Restart preserves instance history across a new `run_id`; execution state reconstructed from Clerk.
11. Foreign Alpaca activity → `ACCOUNT_STATE_UNATTRIBUTABLE`.
12. Unprovable reconciliation → `ACCOUNT_STATE_UNPROVABLE`.
13. StopAndFlatten ends with verified zero attributed exposure.
14. STOP with forbidden carryover and non-zero attributed exposure returns `STOP_REQUIRES_FLATTEN`.
15. Approved carryover STOP records a stable checkpoint; exact Resume succeeds with a new `run_id`.
16. Changed quantity, instrument, config, unresolved intent, or unprovable account truth blocks Resume without mutation.

## Appendix — answers to codex's 12 questions

1. **Clerk sole coordinator?** Yes in principle and already so; bot-local `in_position` and duplicate live strategy math are removed in Slice A.
2. **Immutable config?** Immutable per instance; material change = new `sid`. No versioning.
3. **Account exclusivity?** Yes — Clerk-exclusive; foreign activity → freeze #1. No multi-writer reconciliation.
4. **Uncertain blocks account or instance?** Instance by default; account only when account truth becomes unattributable or unprovable.
5. **Semantic intents or raw legs?** Binary ENTER/EXIT effect operations; Clerk derives side and child order intents.
6. **`decision_id` idempotency boundary?** Yes, for the effect operation; it remains stable across `run_id` restart and composes with zero-to-many child `intent_id` values.
7. **Stop preserves exposure?** Only with approved carryover and a Clerk-backed checkpoint; otherwise it requires explicit StopAndFlatten.
8. **Partial fills in projection?** Existing fold by `(account_id, event_key)` → `ENTRY PENDING (n/m)`; no new store.
9. **Durable strategy activity?** Actionable decisions + folded heartbeat; no per-bar receipt; no strategy checkpoint needed when exact canonical-bar reconstruction and restart-stable decision idempotency are proven.
10. **Journal supports projection by strategy instance?** Yes, already — one execution store.
11. **Checkpoint vs reconstruct on restart?** Execution from Clerk; strategy-clock recomputed from canonical bars; carryover STOP stores a Clerk-backed exposure checkpoint for Resume proof.
12. **PR #1319 keep/rewrite/delete?** Keep the Clerk + namespace + harness; rewrite `bot_trade_strategy.py`; delete bot-local execution truth.
