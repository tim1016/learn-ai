# Alpaca broker execution-layer hardening & 4-bot UI readiness

**Date:** 2026-08-03

**Branch:** `fix/alpaca-bot-control-readiness`

**Method:** Evidence-led audit — four parallel read-only investigators (broker
execution path, clerk/instance authority boundary, UI control-placement +
feedback, scenario catalog + test realism) followed by adversarial
self-verification of every claim against the real code, then the smallest safe
fixes with regression tests.

**Companion documents (one source of truth per concern):**

- [`alpaca-bot-control-panel-architecture-audit-2026-08-02.md`](./alpaca-bot-control-panel-architecture-audit-2026-08-02.md)
  — owns the **panel custody invariants** (P0-1 deploy-rebind, P0-2 clear-hold,
  P0-3 orphaned-stop, P1-1 freshness, P1-2 flatten, P2-3 retire/cancel).
- [`../architecture/alpaca-bot-control-remediation-research-plan-2026-08-02.md`](../architecture/alpaca-bot-control-remediation-research-plan-2026-08-02.md)
  — the gated 5-day remediation program for those invariants.
- [`alpaca-8bot-run-2026-07-30.md`](./alpaca-8bot-run-2026-07-30.md) — the run
  that surfaced defect #10.

**Scope.** This pass covers the layer the 2026-08-02 audit did **not**: the
Alpaca **adapter execution path** (error mapping, capability honesty,
rate-limit handling, journal idempotency), the operator-facing **defect #10**,
and **tomorrow's 4-bot UI-driven readiness**. The panel custody P0s are owned
by the gated research plan and were deliberately **not** hot-fixed here.

**Architectural invariant held throughout.** *The Clerk is the authority over
everything (order registration, classification, journal, reconciliation,
custody, account truth) EXCEPT whether a bot instance is currently running —
that fact is owned solely by the running instance.* Verified structurally
sound: run admission is a two-input decision `evaluate_run_admission(bot,
clerk)` where process-state comes only from the instance registry; the Alpaca
Clerk is in-process and hard-enforces submission authority. **`SOCKET_MISSING`
/ host-daemon journal-cure is an IBKR-only concern and does not apply to the
Alpaca run** — its recovery verbs (`clear_hold`, `reconcile_now`,
`record_inventory_baseline`) are already co-located on the panel.

---

## 1. Fixes landed on this branch

| id | Finding (confirmed) | Fix | Regression tests |
|---|---|---|---|
| **CB1** | A vendor **409** had no branch in `map_api_error` → fell to `BrokerUnavailable` (503), so the Clerk mis-routed a definitive order conflict into the S5 *uncertain*-lookup path and the operator saw "broker outage / retry". | Added a `409 → BrokerOrderRejected` branch (the class was already declared, never raised). The Clerk's existing `except BrokerError` now classifies it as a definitive `SUBMIT_FAILED`. | `test_errors.py` (unit + `not-Unavailable` assertion); `test_orders_endpoint.py::test_conflict_leg_is_definitive_failure_not_uncertain` (real `responses`-mocked 409 end-to-end, asserts no by-client-order-id lookup). |
| **CB2** | `capabilities()` advertised `stop`/`stop_limit`/`trailing_stop`, but `OrderType` has only `MARKET`/`LIMIT` — a caller gating on capability got a false "yes" (constructing a stop leg raises `ValidationError`). | Narrowed `supported_order_types` to `("market","limit")`. | `test_capabilities.py` — pins the advertised set to the constructible `OrderType` enum so they cannot drift. |
| **CB3** | **No rate-limit backoff.** A 429 was parsed (`retry_after_ms`) but never retried; a synchronized N-bot burst would drop that bar's decision indistinguishably from a reject. | Bounded write-path retry (`submit`/`cancel`) honoring `Retry-After`, capped (≤2 retries, ≤1 s each) so it can never stall a bar; on exhaustion a **distinct "throttled" message** surfaces (not a plain reject). Retry is idempotency-safe: a 429 means the order did not land, and a genuine duplicate returns a 409 → definitive (composes with CB1). Reads never retry. | `test_client.py` — retry-then-succeed, exhaust-with-distinct-signal, backoff-capped, cancel-retry, read-does-not-retry. |
| **Defect #10** | The 2026-07-30 Stop **409-storm** dead-ended the operator (forced a raw `POST .../stop`). The documented cause ("whole-panel revision advances faster than the refetch") is **architecturally impossible** — the Stop token keys only on `ctx.running` (`action_policy.py:362`, verified). | (a) **Instrumentation:** a single log line at the pre-execution reject choke point names the 409 subclass (`StaleRevisionError` vs `ActionNotAvailableError`) so a field 409 is attributable. (b) **Token-stability pin:** two contexts differing in every field except `running` yield a byte-identical Stop token. (c) **Frontend resilient retry:** on a 409, refetch the authoritative panel and retry **once** with the current token — but only if the action is still offered, enabled, and its token changed; otherwise re-throw so the operator sees an honest "state changed" message. | `test_action_policy.py` (token stability + non-vacuous control); `broker-v2-panel.service.spec.ts` (retry-on-clearing-409, no-retry-when-disabled, no-retry-when-token-unchanged, non-409 passthrough); shell spec updated to the resilient path. |
| **Env** | `test_missing_credentials_map_to_auth_error` passed in CI but failed locally — `AlpacaSettings` reads `.env` relative to CWD, defeating `delenv`. | `monkeypatch.chdir(tmp_path)` so the settings genuinely find no credentials — host-independent. | The test itself. |

**Files changed:** `app/broker/alpaca/errors.py`, `app/broker/alpaca/broker.py`,
`app/broker/alpaca/client.py`,
`app/services/broker_v2_panel/action_execution_service.py`,
`Frontend/.../lib/broker-v2-panel.service.ts`,
`Frontend/.../panel-shell/bot-panel-shell.component.ts`, plus their tests.

---

## 2. Investigated → disproven (no code change; adversarial verification)

Two subagent-flagged "likely bugs" did **not** survive verification against the
real code. Documented here so they are not re-investigated, each now pinned by a
regression test.

- **CB4 — "journal double-count across restart corrupts FIFO P&L": DISPROVEN.**
  A redelivered fill crossing a data-plane restart *does* append a duplicate
  `ORDER_EVENT` line, but every numerical consumer dedups on `(account_id,
  event_key)` — `fills.py:project_instance_fills`, `exposure.py`,
  `rollup_cache.py` — and this is already covered by
  `test_fills.py::test_duplicate_event_key_deduped` and
  `test_fifo_pnl.py::test_duplicate_event_key_is_idempotent`. **Residual
  (minor):** a duplicate *journal line* is numerically harmless but could show a
  fill twice in the raw evidence rail, and `skipped_duplicate` under-counts on a
  post-restart redelivery. **Not fixed tonight:** the only real fix — seeding
  the consumer's in-memory dedup from a full journal replay with reconstructed
  fingerprints — is risky in a delicate dedup path (a mis-seeded fingerprint
  would drop a legitimate fill entirely, worse than a duplicate display line)
  and belongs with the research plan's journal event-identity work
  (Workstream C/D).

- **LB2 — "gap-reconcile synthesizes a fill without top-level qty/price →
  per-instance P&L drop": DISPROVEN.** `_order_to_event_payload`
  (`trade_updates.py:719-721`) explicitly sets top-level `price`/`qty` for
  `fill`/`partial_fill`, and `from_alpaca_trade_update` (`adapter.py:351-352`)
  reads exactly those top-level fields. Pinned by the new composition test
  `test_gap_reconciled_fill_carries_execution_qty_and_price`.

---

## 3. Deferred to the gated research plan (documented, not hot-fixed)

These overlap the 2026-08-02 audit and its 5-day program, which explicitly
forbids opening a Start/Stop/Deploy/Clear-Hold implementation before its
research gate. Hot-fixing them would risk the very custody invariants they
protect.

| Finding | = Audit item / Workstream | Why deferred |
|---|---|---|
| Flatten renders green **success** for `UNPROVABLE`/submitted outcomes | P1-2 / B3 (typed command state machine) | The performer *message* is already honest; only the visual overstates. An honest fix needs a typed-outcome field on `PanelActionResult` (a contract change) — the B3 redesign. Shortcuts rejected: FE message-parsing is fragile; raising an error from the performer for an in-progress state **burns the idempotency key** (`action_execution_service.py:361-364`). |
| `clear_hold` clears without re-proving the root condition | P0-2 / A3 | Designed `HoldClearAdmission` under the intake lock. |
| Orphaned task after a failed Stop (no run-generation fence, no UI force-kill) | P0-3 / A2 | Run-generation fencing at the Clerk boundary; gate-blocked. |
| 24 h display freshness reused for control admission | P1-1 / B1 | Producer-derived operation-specific TTLs. |
| No per-bot `retire`/`cancel_order` on the Alpaca panel | P2-3 / E4 | Vertical-slice mini-designs. |

---

## 4. Tomorrow — 4-bot UI-driven rehearsal runbook

Goal: exercise **many failure scenarios**, prove both authority layers are
enforced, and confirm **correct UI feedback with zero CLI drop**. Alpaca
**paper only**; 4 bots (fewer bots, more scenarios).

| # | Scenario | How to induce (safe, paper) | Expected UI feedback | Authority / observability check |
|---|---|---|---|---|
| 1 | Broker reject (422) | Deploy a bot sized past buying power; let it try to submit. | Leg fails with a specific *what/why* ("insufficient buying power"), **not** an outage. | Clerk journals `SUBMIT_FAILED`; no order lands. |
| 2 | **Order conflict (409)** | Hard to force on paper; watch the logs. | If it occurs: a definitive conflict message, not "broker outage / retry" (CB1). | `map_api_error` → `BrokerOrderRejected`; Clerk `SUBMIT_FAILED` (definitive), no uncertain lookup. |
| 3 | Rate-limit (429) | Unlikely at 4 bots (≪ 200/min); watch for it under bursts. | A distinct **"throttled … it did not land"** message after bounded retries — never a silent lost bar (CB3). | Logs: `rate_limit_retry` → `rate_limit_exhausted`. |
| 4 | **Stop a hot bot** | Click Stop repeatedly on an actively-trading bot. | Stop **succeeds** (FE refetches a fresh token and retries once on a transient 409). If it genuinely can't stop, an honest "state changed" message — no dead-end (defect #10). | Log `panel_action_rejected` names the 409 subclass (`StaleRevisionError` vs `ActionNotAvailableError`) — **capture which one appears** to finally attribute the 2026-07-30 storm. |
| 5 | Partial fill | Small marketable order in a thin name. | `filled X / total` surfaced honestly. | Exposure/FIFO fold counts the execution slice. |
| 6 | Cancel a working order | Rest a limit order, then cancel. | Cancel from the account desk manual ticket (per-bot cancel is deferred — see §5). | Clerk journals `cancel`; a throttled cancel now retries (CB3). |
| 7 | Flatten with unprovable outcome | Flatten while a reducing order is only working. | **Read the receipt message** — it says "await durable fill receipt" / "cannot prove flat". The green check overstates (deferred, see §5). | `EffectOperationState` is honest at the Clerk; the visual is the only gap. |
| 8 | Reconciliation drift | Place a foreign order in the account outside the bot. | Hold raised; `clear_hold` co-located on the Clerk card. | Clerk `UNEXPLAINED_ORDER_HOLD`; submits blocked, cancels allowed. |
| 9 | Deploy in each execution mode | Deploy once per mode. | Confirm the mode is not a silent no-op (the 2026-07-30 `log_only` surprise). | Verify the deploy vocabulary matches the runtime mode end-to-end. |
| 10 | **Data-plane restart mid-run** | Restart `polygon-data-service` while bots hold exposure. | After restart, P&L/exposure intact; no phantom double fill in numbers. | `event_key` dedup absorbs any redelivery (CB4 disproof); a duplicate *line* is harmless. |

---

## 5. Known limitations to brief the operator before the run

- **Flatten green check can overstate.** Trust the receipt *message*, not the
  check — "await durable fill receipt" / "cannot prove flat" means **not yet
  flat**. (Deferred: P1-2 / B3.)
- **No per-bot `retire` / `cancel_order` on the Alpaca panel.** To cancel a
  stray order, use the account desk's manual order ticket — a UI path, not a CLI
  drop. (Deferred: P2-3 / E4.)
- **Orphaned-task-after-failed-Stop (P0-3) is unfixed.** If Stop reports success
  but a bot still appears to act, escalate to a controlled process restart; the
  race is narrow and unconfirmed at runtime. (Deferred: P0-3 / A2.)
- **Rate-limit retry is bounded** (≤2 attempts, ≤1 s each). A sustained throttle
  surfaces as a distinct "throttled" failure for that bar — bounded, honest, and
  never silent.
- **Defect #10 root cause is still unconfirmed** — the instrumentation exists to
  attribute it during the run. The FE retry makes Stop reliable regardless.

---

## 6. Validation performed

| Check | Result |
|---|---|
| `ruff check app/ tests/` (project scope) | **Passed** |
| Python — touched surfaces (`tests/broker/alpaca/`, `tests/broker/v2panel/test_action_policy.py`, `test_action_execution.py`) | **402 passed, 1 inherited failure** (below) |
| Frontend — touched specs (v2-panel service + panel-shell) | **18 passed** |
| `eslint` on touched frontend files (`--max-warnings 0`) | **Passed** |

**Inherited (pre-existing) failure, surfaced per pre-push hygiene:**
`tests/broker/v2panel/test_action_execution.py::test_live_panel_skips_resume_admission_reconciliation`
fails on `master` too (baselined via stash) — a stale test monkeypatch whose
`_read_order_journal` lambda takes one arg while production passes two. It is in
a file this branch never touched; not addressed here.

Full project-scope frontend `eslint` + the `thermo-nuclear-code-quality-review`
skill are the remaining gates before opening a PR.
