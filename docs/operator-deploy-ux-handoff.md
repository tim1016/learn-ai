# Handoff — operator deploy/operations UX gap

**Date:** 2026-05-31
**Status of the console redesign:** DONE. The 9-issue operator-console redesign
(the grilling-session findings → ADR 0004/0005) shipped: 8 AFK slices merged
(PRs #401–#409), plus the HITL cutover staged as **PR #410** (`ready-for-human`,
not merged). See `CONTEXT.md` + `docs/architecture/adrs/0004-*.md` / `0005-*.md`.

This handoff is about the **gap that redesign did NOT cover**: deploying and
operating a strategy *from the UI*, with first-class error messaging.

---

## The gap, precisely

The instance console (`/broker/instances`, `broker-instances` component) is an
**observe + control** surface for instances that already exist. It cannot
**deploy** (create) or, after the cutover, **launch** a strategy.

Deploy is a **3-stage pipeline**; the UI only ever covered stage 2, and PR #410
retires even that:

| Stage | Mechanism | UI today |
|---|---|---|
| 1. Create the run (`init-ledger`) | CLI `python -m app.engine.live.run init-ledger …` — writes `run_ledger.json` (spec + account + QC backtest ref + `strategy_instance_id`) | **None — CLI only, no API** |
| 2. Launch the process | host daemon `POST /runs/{run_id}/start` | Had a **Start button** on the old `broker/paper-run` page; **PR #410 redirects that page away and did NOT port Start/Stop to the console** |
| 3. Observe/control | the instance console | ✅ built (intent knob, one-shot commands, readiness, broker slice, fleet contamination) |

Also: the **host daemon is a host process, not in compose** — `python -m
app.engine.live.host_daemon` must be running. It wasn't during the last session,
which is why `/broker/instances` showed "No strategies found" (`GET
/api/live-instances` → `[]`, zero run dirs, daemon unreachable). **That empty
state is correct, not a bug.**

`init-ledger` required args (so a create-form must collect all of these):
`--repo-root --strategy-spec-path --account-id --start-date-ms
--qc-audit-copy-path --qc-cloud-backtest-id` (+ optional `--strategy-instance-id
--live-config-json --run-root --force`). The **QC-backtest args are required by
design** — every live run is anchored to a QuantConnect Cloud backtest for
three-way reconciliation. So "deploy" is *not* one-click; it is reconciliation-gated.

---

## What to build (two pieces)

**A) Port Start/Stop into the console (fixes PR #410).** Small. The service
methods already exist: `LiveRunsService.startHostRunner(runId, request)` /
`stopHostRunner` / `getHostRunnerHealth`. The old page's "Host Runner" card
(`broker-paper-run.component.html` ~line 99/143) is the reference UI: Start with
`{strategy, readonly, hydrate_policy, max_orders_per_day, ibkr_host}`, Stop,
daemon-health display. **#410 should not merge until this is ported**, or the
console loses the only Start affordance.

**B) Deploy form + a server-side create endpoint (the real gap).** New work:
- A new `POST /api/live-instances` (or `/api/live-instances/{id}` create) that
  runs `init-ledger` server-side and optionally starts it. **No create API exists
  today** — `init-ledger` is CLI only.
- A "Deploy strategy" form: pick spec → account → readonly/hydrate/max-orders →
  **QC backtest reference** → name the instance.

---

## Facts gathered for the deploy form (verified)

- **Strategy specs ARE discoverable**: `GET /api/engine/strategies` →
  `list[StrategyInfo]`. Spec fixtures live in
  `app/engine/strategy/spec/fixtures/*.spec.json` (spy_ema_crossover,
  rsi_mean_reversion, sma_crossover). A deploy form can populate a spec dropdown
  from this — but confirm it returns the *spec path*, not just a name.
- **Accounts**: `GET /api/broker/account` → `IbkrAccountSummary` (the connected
  broker's account). The paper account id (DU…) comes from the live IBKR
  connection — so the form needs the broker connected, or a manual account field.
- **QC Cloud backtest: NO API integration exists.** No endpoint lists/selects QC
  backtests. The `qc-audit-copy-path` + `qc-cloud-backtest-id` are operator-
  supplied via CLI today. **This is the hard part of a deploy form** — where does
  the QC reference come from in the UI? (manual entry? a new QC-cloud listing
  integration? relax the requirement for paper/shadow?) — a key grill question.
- **No centralized frontend error pattern.** No `MessageService`/toast/global
  `ErrorHandler` in the broker components; the old page used ad-hoc `writeError`
  signals. "Good error messaging" needs a *deliberate* pattern — there is no
  existing one to follow.

---

## The error-messaging plan to grill on (the user's real goal)

The user wants "UI UX around all operations with good error messaging." The
console already has a strong precedent: the **readiness gate** (ADR 0005) — a
structured `{verdict, gates:[{name,status,severity,detail}]}` that says *why
blocked and what each gate means*. The thesis to grill: **every operation should
follow that same pattern** — precondition → action → pending → structured
result/error with "what failed + why + what to do next".

Decision tree to resolve in the grill (one at a time, each with a recommended
answer, explore codebase first):

1. **Scope / operations inventory.** Confirm the full set: deploy(create),
   launch(start), stop, pause/resume(intent), one-shot commands
   (flatten/reconcile/mark-poisoned), retire/decommission. Which get UI now vs
   later?
2. **The QC-backtest requirement in a deploy form.** Manual entry vs new QC
   listing vs relax-for-paper. (Load-bearing — blocks the deploy form.)
3. **Error taxonomy + surfacing.** Categories: validation / precondition-not-met
   / transient-infra (daemon down, broker disconnected) / domain-rejection
   (409 no live binding, poisoned). Inline-per-operation vs global toast vs both.
   Recommended: extend the readiness-gate "why + what to do" model to all ops.
4. **Precondition affordance: disable-with-reason vs allow-and-explain.** The
   console already disables (commands disabled w/o live binding). Make the
   *reason* always visible (tooltip/inline), never a silent disabled button.
5. **Infra-state distinction.** The UI must cleanly separate "nothing deployed"
   from "daemon unreachable" from "broker disconnected" — today these all collapse
   to an empty/`unreachable` state. A first-class connectivity/health strip.
6. **Create endpoint shape + idempotency + where init-ledger runs** (data plane
   vs host daemon — note init-ledger needs `--repo-root`/git clean-tree checks).

Offer ADRs sparingly: a "deploy/create control plane" decision (where init-ledger
runs as an API, QC-ref handling) is likely ADR-worthy.

---

## Resolution (grilling session 2026-05-31)

Decisions reached. These supersede the open questions above where they conflict.

**Scope of this effort: A + B now, C deferred to a separate ADR.**
- **In:** (A) the cross-cutting error-messaging pattern, and (B) porting Start/Stop
  into the console.
- **Out (own ADR):** (C) deploy/create form + `POST /api/live-instances`. It is *not*
  UX work — `init-ledger` runs a git clean-tree check and hashes `git HEAD` +
  `qc-cloud-backtest-id` (no API) into the `run_id`. A Deploy button means an HTTP
  request triggers git ops on the working tree and demands a QC ref with no source.
  That is a control-plane decision, now resolved in
  **`docs/architecture/adrs/0006-deploy-control-plane-host-daemon-init-ledger.md`
  (Accepted 2026-05-31)**: host-daemon deploy (only the host has the git tree; the
  data-plane container has no `.git`), content-addressed `run_id` as the idempotency
  key, QC anchor preserved (relax-for-paper rejected), and **QC ref sourced by manual
  entry for v1** (backtest-id text + host-side picker scoped to `references/qc-shadow/`;
  QC-cloud listing integration deferred).

**Error messaging:**
- Wire stays **strings-only** — `HTTPException(detail=...)` is left unchanged; no
  structured error envelope is retrofitted onto the live endpoints.
- The frontend derives category + "what to do next" from an **`(operation, HTTP
  status)` lookup**, never by parsing the `detail` string. The backend `detail`
  renders only as the literal detail line. (Robust to backend wording drift; the
  remediation lives co-located with the UI that needs it.) Existing status-code
  semantics to key off: 400 validation, 404 not-found, 409 domain-rejection
  (no live binding / poisoned), 503 infra (daemon/subprocess down).
- Surfacing is **inline only** — no toast/global notification service. Accepted
  consequence: async/background failures already have inline homes — the command
  timeline (`status === 'failed'`) and process-state badges (`unreachable`). No
  orphan async errors.
- Preconditions: **disable + always-visible reason** — disabled control, with the
  reason rendered adjacent (not a tooltip, never a bare greyed button). Reuses the
  readiness-gate "why + what to do" framing.
- **Shared connectivity strip on all broker pages**, aggregating daemon `/health`,
  broker `GET /api/broker/account`, and fleet `GET /api/live-instances/account`
  (contamination + `policy_blocks_starts`). This is the single source of truth that
  lets the disabled-control reasons name the *actual* blocker — nothing-deployed vs
  daemon-down vs broker-down vs policy-block — instead of collapsing to one fuzzy
  `unreachable`/empty state. Per-control reasons read the same signals; no per-page
  re-derivation.
- Rollout shape (big-bang vs incremental) left open; default to building the
  error-map util + connectivity strip + inline-result component as shared pieces,
  wired into the operation-bearing pages (`broker-instances`, `broker-orders`)
  first, then adopted elsewhere on touch.

**Start/Stop port (piece B):**
- Port the old "Host Runner" card (all five fields: `strategy`, `readonly`,
  `hydrate_policy`, `max_orders_per_day`, `ibkr_host`) into the console, **but
  default each field from the selected instance's ledger** rather than from blank /
  hardcoded constants.
- **CONFIRMED FOOT-GUN:** `run start` imports the algorithm purely from
  `--strategy` (`run.py:647–662`) and loads the ledger's spec *separately*
  (`run.py:778`), only for the decision schema. There is **no cross-check** that
  `--strategy` matches the spec the `run_id` was hashed from. A mismatched
  `--strategy` silently runs a *different algorithm* against a ledger reconciled to
  a different QC backtest — breaking the three-way reconciliation guarantee.
  Ledger-defaulting the form field is the only UI-side guard; consider also adding
  a validation in `run start` (or the daemon's `/runs/{run_id}/start`) that rejects
  a `strategy` inconsistent with `ledger.strategy_spec_path`.

**Sequencing:**
- The Start/Stop port goes **into PR #410 before it merges** (or a PR that merges
  atomically with it). #410 must never land in a state where the UI cannot start a
  run — it retires the only existing Start affordance.

---

## Pointers

- Console: `Frontend/src/app/components/broker/broker-instances/` (component +
  template + spec); service `Frontend/src/app/services/live-runs.service.ts`;
  types `Frontend/src/app/api/live-instances.types.ts`.
- Old page (Start/Stop reference, retired by #410):
  `Frontend/src/app/components/broker/broker-paper-run/`.
- Backend: `PythonDataService/app/routers/live_instances.py`;
  `app/engine/live/host_daemon.py` (the daemon + start/stop);
  `app/engine/live/run.py` (`init-ledger` / `start` CLI subcommands).
- Design: `CONTEXT.md`, ADR 0004 (control plane), ADR 0005 (readiness/broker).
- Open PR to finish: **#410** (cutover) — port Start/Stop before merge.
