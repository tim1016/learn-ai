# Clerk ↔ Broker custody resolution — design

- **Date:** 2026-08-03
- **Status:** approved (design); implementation not started
- **Surface:** Alpaca Accounts page (`/brokers/alpaca`) + per-bot Operator lens (parity)
- **Motivating case:** paper account `PA3KWXU1C4C3` froze ("new entries blocked", reconciliation "Missing Intent") because the Clerk attributes **SPY 2** while the broker holds **SPY 1**, left by a 2026-07-31 run that "Exited Unverified — Interrupted By Restart." The only resolution today is two verbs buried in the per-bot Operator lens, with **no operator-comment capture** and **nothing on the Accounts page**.

## 1. Problem & goal

The **Clerk is the source of truth for bot runs.** An unclean shutdown / interrupted run can leave the Clerk out of sync with the broker's actual account state. Today that is only resolvable through per-bot Operator-lens verbs (`reconcile_now`, `record_inventory_baseline`, `clear_hold`), which:

- are not exposed on the account-level **Accounts page** where an operator manages custody;
- capture **no operator reason** — the `reason` slot exists end-to-end but is dead-wired (frontend sends `null`, backend drops it, performers use hardcoded literals);
- offer no **explanation** of *what* diverged or *why* it might have happened.

**Goal:** one operator-driven surface on the Accounts page to **detect, explain, and resolve** Clerk↔broker divergences — bringing the Clerk back in sync with the broker, with a **required operator comment permanently recorded in the Clerk journal**. The operator's role is **confirm + explain**; the machine diagnoses and executes.

### Non-goals (this cut)

- Flattening positions. "Sync to broker" adopts broker truth into the Clerk; deciding to *flatten* the resulting exposure is a separate trading action (Flatten & stop / manual order ticket).
- A live-trading stronger-review policy. We add a `resolution_posture` seam so live can demand more later, but v1 implements only the **paper** posture.
- A full custody-resolution history/audit browser. The journal records everything; a dedicated history view is future work.
- Resolving divergences no Clerk verb can prove (see §3 escalation).

## 2. Concept & resolution model

1. Backend **diagnoses** the account → resolution **plan** (ordered recovery steps) + **backend-authored explanation** (grounded in the run evidence) + **example possible causes**.
2. The Accounts page shows **sync status**. When diverged: the explanation, the exact **clerk-vs-broker delta**, the **possible causes**, and one primary **"Resolve & sync to broker"** action.
3. Click → confirm dialog repeats the delta + plan, **requires an operator comment** ("why did this happen?"), plus a typed-token confirm (`RESOLVE`).
4. Confirm → backend executes the plan **atomically against a snapshot version** (409 if broker state changed since diagnosis), **journals the operator comment** on the mutating steps, returns a **receipt**.
5. Result: **Clerk == broker, account unblocked, the "why" permanently recorded.**

## 3. Divergence classes & escalation

Bounded to what the Clerk's verbs can actually **prove and resolve**:

| Divergence `kind` | Example | Resolution step |
|---|---|---|
| `EXPOSURE_ATTRIBUTION_MISMATCH` | Clerk attributes SPY 2, broker holds SPY 1 | `record_inventory_baseline` (adopt broker positions as truth, retire stale attribution) |
| `EXPOSURE_HOLD` | `UNEXPLAINED_ORDER_HOLD` blocks new entries | `clear_hold` |
| `STALE_RECONCILIATION` | Clerk hasn't swept the broker recently | `reconcile_now` (read-only; usually the diagnostic pre-step) |

Each divergence resolves to exactly one of three **states**:

- **resolvable-now** — a plan exists and its guards are READY; the guided action runs.
- **blocked-on-prerequisite** — a plan exists but a guard blocks it. These come straight from the existing `_guard_record_inventory_baseline` blockers and are *"clear this first,"* not permanent escalation:
  - `BOT_RUNNING` → stop the bot first (can't baseline under a live writer);
  - `WORKING_ORDERS_PRESENT` → cancel/await the open orders (baseline can't adopt a moving position);
  - `UNRESOLVED_INTENTS` → the reconcile pre-step must sweep them first.
  The UI disables the primary action and surfaces the backend `blocked_reason` with a one-click to the prerequisite (Stop / Cancel) where one exists.
- **needs-review** — the only true escalation: an **unresolved intent the broker sweep cannot map to any outcome** (Clerk submitted; broker shows neither an order nor a fill). No verb can invent the truth → surfaced as "cannot prove — manual review," **no fake resolution**.

`resolution_posture`: `paper` (v1) requires *comment + typed token + snapshot guard*. `live` (future) can plug in a stronger bar (dual sign-off, or refuse a blanket baseline) on the same flow without reshaping it.

## 4. Backend design (data-plane `:8000`)

New account-scoped endpoints on the existing `PythonDataService/app/routers/brokers.py` router (prefix `/api/brokers`), keyed by `{broker}` exactly like the existing `POST /api/brokers/{broker}/clerk/clear-hold` (single configured Alpaca account; extensible to an `account_id` path later).

**Auth follows the existing `brokers.py` clerk endpoints:** the `GET` diagnosis is a protected data-plane read (`PROTECTED_DATA_PLANE_READ_DEPENDENCIES`, `main.py`); the `POST` resolve is an unsafe control mutation behind the data-plane control secret (the Angular proxy attaches `X-Data-Plane-Control-Secret` for marked control mutations). Response models use snake_case fields (the .NET / OpenAPI consumers expect it).

### 4.1 Diagnosis — `GET /api/brokers/{broker}/clerk/custody-diagnosis`

New response model `CustodyDiagnosis` (`app/schemas/…`):

- `in_sync: bool`
- `observed_at_ms: int` (int64 ms UTC)
- `snapshot_version: str` — stable hash of the (broker positions + clerk attributed state + hold + working orders) snapshot; the concurrency guard.
- `resolution_posture: Literal["paper", "live"]`
- `resolvable: bool`, `blocked_reason: str | None`
- `divergences: list[Divergence]`, each:
  - `kind: Literal[...]` (the `kind`s in §3; code-like → `receiptLabel` on the client)
  - `state: Literal["resolvable_now", "blocked_on_prerequisite", "needs_review"]`
  - `explanation: str` — backend-authored prose, grounded in evidence
  - `possible_causes: list[str]` — backend-authored
  - `delta: dict` — structured; for attribution mismatch: `positions: [{ symbol, clerk_attributed_qty, broker_observed_qty }]`
  - `resolution_step: ActionId | None`
  - `prerequisite: { blocker_code, detail, one_click_action?: ActionId } | None`
  - `evidence_refs: list[str]` — opaque ids preserved exactly
- `resolution_plan: list[{ action_id: ActionId, scope: Literal["bot","account","broker"], mutates: bool }]` — ordered; empty when in-sync or nothing resolvable.

**Computation:** compare Clerk attributed exposure (Clerk ledger/state) vs broker positions; check hold state and reconcile freshness; map to `kind`/`state`. Reuse the existing per-bot readiness logic (`app/services/broker_v2_panel/panel_projection_service.py:_readiness_checks`, guards in `app/broker/v2panel/action_policy.py`) but projected at **account** scope. No new persisted state.

### 4.2 Resolve — `POST /api/brokers/{broker}/clerk/resolve`

Request `CustodyResolutionRequest` (mirrors `ClearHoldRequest`, `brokers.py:240-262`):

```python
model_config = ConfigDict(extra="forbid")
reason: str = Field(min_length=1, max_length=512)   # REQUIRED, non-blank
snapshot_version: str = Field(min_length=1)          # must match current diagnosis
confirmation_token: str = Field(min_length=1)        # typed "RESOLVE"
idempotency_key: str = Field(min_length=1, max_length=128)
# operator injected server-side (PANEL_OPERATOR_IDENTITY), not a request field
```
A `field_validator` strips and rejects blank `reason` (copy `brokers.py:_reason_is_nonblank`).

**Handler:**
1. Re-diagnose. If `snapshot_version` != current → **HTTP 409** (state changed; re-diagnose). If `confirmation_token != "RESOLVE"` → 422.
2. If already `in_sync` → idempotent no-op receipt.
3. Execute `resolution_plan` in order. For each mutating step pass `operator` + the operator's `reason`:
   - `reconcile_now` → `clerk.reconcile_once()` (read-only; no reason slot — not journaled with a comment)
   - `record_inventory_baseline` → `clerk.record_inventory_baseline(operator=operator, reason=reason, …)` (account-scoped — see accommodation below)
   - `clear_hold` → `clerk.clear_hold(operator=operator, reason=reason)`
4. Return `CustodyResolutionReceipt`:
   - `resolved: bool`, `new_sync_status: { in_sync, observed_at_ms }`
   - `steps_executed: [{ action_id, journal_ref? }]`
   - `remaining_divergences: list[Divergence]` (empty on full success)
   - `receipt_id: str`, `recorded_at_ms: int`

### 4.3 Journaling — no storage schema change

The operator comment lands on the mutating steps' **existing** slots:

- `OrderJournalEntry.operator` + `.reason` on `HOLD_CLEARED` and `BROKER_EVIDENCE_BASELINE` (`app/broker/alpaca/clerk/models.py:215,261`; validators already **require** both — `models.py:359-362`).
- `reconcile_now`'s `RECONCILIATION` row has no operator/reason slot (`models.py:353-355`) — it's read-only and never *is* the fix, so nothing is journaled there. A real sync always includes a mutating step, so the required comment always lands on a durable record.

### 4.4 Copy — backend-authored

A `custody_diagnosis_copy` map keyed by `kind` provides `explanation` templates and `possible_causes`, with evidence interpolated (e.g., the terminal-evidence timestamp / "Interrupted By Restart"). Frontend renders these strings verbatim (as it already does for readiness `explanation`/`cure`). New **code-like** tokens (divergence `kind`) are added to the vocabulary/`receiptLabel` contract (`app/broker/v2panel/vocabulary.py` + `broker-v2-emergency-copy.ts` + `broker-v2-vocabulary.snapshot.json` + `broker-v2-copy-contract.spec.ts`).

### 4.5 Accommodations (net-new, contained)

1. **Account-scoped baseline without a bot `sid`.** `clerk.record_inventory_baseline(...)` currently takes `strategy_instance_id`. An account cutover retires prior bot attribution and isn't one bot, so the resolve path invokes it **account-scoped with a `None`/sentinel `sid`**; if the clerk method rejects that today, add an account-scoped entry point (or default) rather than picking an arbitrary bot. The journal record is already account-scoped (`BROKER_EVIDENCE_BASELINE` carries `account_id`), so no record-shape change is implied.
2. **Snapshot-version guard.** Diagnosis emits `snapshot_version`; resolve validates it (409 on mismatch). Prevents resolving against stale evidence.

## 5. Frontend design

### 5.1 New component

`AlpacaDeskComponent` (`Frontend/src/app/components/brokers/alpaca-desk/`, a 41-line pure composer) gains `<app-alpaca-custody-resolution />`, slotted near the top by `<app-alpaca-hold-banner />` (custody sync is a whole-account concern). New files: `alpaca-custody-resolution.component.{ts,html,scss}`. Injects `BrokersService` with two new methods:

```ts
getCustodyDiagnosis(broker = 'alpaca'): Promise<CustodyDiagnosis>          // GET  /api/brokers/{broker}/clerk/custody-diagnosis
resolveCustody(broker, body: CustodyResolutionRequest): Promise<CustodyResolutionReceipt>  // POST /api/brokers/{broker}/clerk/resolve
```
Diagnosis loads via `resource()`; a refresh re-fetches (also called after resolve and on 409).

### 5.2 Four states (from the diagnosis)

- **In sync** — quiet strip: "Clerk and broker are in sync · checked {ts}".
- **Diverged, resolvable-now** — prominent card: header, backend `explanation`, the **delta table** (per-symbol Clerk-attributed vs broker-observed), **possible causes** list, **plan preview** ("Resolve will: reconcile → adopt broker inventory as baseline …"), primary **"Resolve & sync to broker"** button.
- **Diverged, blocked-on-prerequisite** — same card, primary button disabled, backend `blocked_reason` shown with a one-click to the prerequisite (Stop → the bot's panel; Cancel → the order ticket) when available.
- **Needs-review** — "Cannot prove intent {ref} — manual review required" with `evidence_refs`; no primary button.

### 5.3 Confirm dialog

New focused `custody-resolution-confirm-dialog` co-located in `alpaca-desk/` (cloning the required-reason markup from `account-desk-recovery-confirm-dialog.component.html:53-79` rather than generalizing that dialog — it is tightly coupled to the v1 `AccountDeskRecoveryStore`; a clone keeps boundaries clean):

- Repeats the delta + plan.
- **Required comment** `pTextarea`: label *"Why did the Clerk and broker fall out of sync?"*, help *"Required. This becomes part of the audited Clerk recovery record."*, `[invalid]="reason.trim().length === 0"`, aria-required/aria-invalid, `role="status"` blocking message.
- **Typed-token** confirm: type `RESOLVE`.
- Confirm disabled until comment non-blank **and** token matches.
- Submit → `resolveCustody(...)`.
- **409** → honest "account state changed — re-checking," auto re-fetch diagnosis, operator re-confirms (never silently re-fire the mutation).
- **Success** → receipt (reuse `ActionReceiptView` shape / `PanelActionReceiptComponent`): "Synced — adopted broker inventory, cleared hold. Account clean." + refresh diagnosis, account card, positions.

### 5.4 Copy, timestamps, a11y

- Backend prose (`explanation`, `possible_causes`, plan text, `blocked_reason`) rendered verbatim/unpiped; code-like tokens (`kind`, `scope`, reason codes) through the `receiptLabel` pipe; opaque ids/refs (`evidence_refs`, `receipt_id`) preserved exactly.
- All timestamps are int64 ms UTC on the wire; rendered by the shared timestamp-display component (instants viewer-local per operator preference).
- AXE / WCAG AA: the card is a labelled region; the dialog traps focus; aria-required/aria-invalid on the comment; `role="alert"` errors; every control has an accessible name.

### 5.5 Full-parity — un-dead-wire the per-bot comment

So resolving from the **per-bot cockpit** captures the comment identically:

- **FE:** `BrokerV2PanelService.submitAction` (`broker-v2-panel.service.ts:219-233`) sends the operator's `reason` instead of hard-`null`; the Operator-lens gate confirm (`operator-readiness.component`) gains the same required-comment textarea for the mutating verbs (`record_inventory_baseline`, `clear_hold`).
- **BE:** thread `PanelActionRequest.reason` through `action_execution_service.execute_action` (`:357`) → performer (`panel_data_source.py:833-864`) → `clerk.*(reason=…)`, replacing the hardcoded literals; require `reason` for those two `action_id`s, keep it optional for non-mutating verbs.

## 6. Data flow (end-to-end, happy path)

1. Accounts page mounts → `getCustodyDiagnosis()` → backend compares Clerk vs broker → returns `CustodyDiagnosis { in_sync:false, divergences:[EXPOSURE_ATTRIBUTION_MISMATCH …], plan:[reconcile_now, record_inventory_baseline], snapshot_version }`.
2. Card renders explanation + delta (SPY 2/1) + causes + plan.
3. Operator clicks Resolve → dialog → types reason + `RESOLVE` → `resolveCustody({ reason, snapshot_version, confirmation_token, idempotency_key })`.
4. Backend re-diagnoses, matches `snapshot_version`, runs `reconcile_once()` then `record_inventory_baseline(operator, reason)` → journals `BROKER_EVIDENCE_BASELINE { operator, reason }` → returns receipt `{ resolved:true, new_sync_status:{ in_sync:true } }`.
5. FE shows receipt, re-fetches diagnosis (now in-sync), refreshes account card + positions.

## 7. Error handling

- **Blank/whitespace reason** → 422 (validator) + FE confirm disabled (defence in depth).
- **Stale snapshot** → 409 → FE re-diagnoses, operator re-confirms; the mutation is never auto-retried.
- **Prerequisite blocker** (running bot / working orders / unresolved intents) → diagnosis returns `blocked_on_prerequisite`; resolve refuses (guard) with the backend reason; FE disables the button and points to the one-click.
- **Idempotency** → `idempotency_key` makes a repeated resolve a no-op; an already-in-sync account resolves to a benign no-op receipt.
- **Partial plan failure** (a later step fails after an earlier mutated) → receipt reports `steps_executed` + `remaining_divergences`; FE shows what synced and what remains; no silent success.

## 8. Testing

- **Backend (pytest):**
  - Diagnosis: in-sync; attribution mismatch → correct `delta`/`plan`/`explanation`; hold; stale; blocked-on-prerequisite (each blocker); needs-review (ambiguous intent).
  - Resolve: happy path journals `operator`+`reason` on baseline/clear-hold; snapshot-stale → 409; blank reason → 422; already-in-sync → idempotent no-op; escalation → refused; partial-failure receipt shape.
  - Parity: `PanelActionRequest.reason` reaches the performer and the journal; required for mutating verbs; optional for others.
- **Frontend (Vitest / Testing Library):** the four states render from a faked diagnosis; dialog gates confirm on comment+token; 409 triggers re-diagnosis; receipt renders; copy discipline (backend prose unpiped, tokens piped). Per-bot gate confirm carries the comment.
- **Contract:** vocabulary snapshot + `receiptLabel` contract updated for new `kind` tokens; OpenAPI contract regenerated for the two new endpoints (CI "Verify committed OpenAPI contract" gate).

## 9. Repo-rule compliance

- **Temporal:** all wire/stored timestamps int64 ms UTC; UI via the shared timestamp component; no ISO/`DateTime` on the wire.
- **Receipts:** code-like tokens through `receiptLabel`; opaque audit tokens preserved; backend-authored prose unpiped.
- **Backend-authored copy:** explanation/causes/plan/blocked_reason from the backend copy map, never client-fabricated.
- **No journal schema change:** reuse existing `operator`/`reason` slots.
- **Boundary validation:** `extra="forbid"`, required non-blank `reason`, snapshot guard, typed token.
- **No new deps.** Reuse PrimeNG `pTextarea`, `resource()`, existing receipt/dialog patterns.

## 10. Implementation slices (tracer-bullet vertical)

- **Slice 1 — Read-only diagnosis, end-to-end.** BE `GET /clerk/custody-diagnosis` (+ copy map, snapshot_version, account-scoped readiness projection) → FE account-page card rendering the four states read-only. Proves the pipeline and UI shell.
- **Slice 2 — Guided resolve.** BE `POST /clerk/resolve` (required comment + snapshot guard + orchestrated plan + account-scoped baseline accommodation, journaling the comment) → FE "Resolve & sync" button + confirm dialog + 409 handling + receipt. Guided resolution works end-to-end.
- **Slice 3 — Per-bot parity.** Un-dead-wire `PanelActionRequest.reason` BE→FE; Operator-lens gate confirm required comment for mutating verbs. Both surfaces capture the comment identically.

Each slice ships BE + FE + tests and leaves the app working.

## 11. Future (out of scope now)

- `resolution_posture = "live"` stronger-review policy (dual sign-off / no blanket baseline).
- Custody-resolution history view on the Accounts page (journal already records it).
- Extending the diagnosis to divergence classes beyond the three Clerk-provable ones.
