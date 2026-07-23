> **Status:** Archived / superseded (2026-07-22).
> **Do not use as implementation authority or an operator procedure.**
> **Current authority:** `docs/bot-control-operator-manual.md`, ADR-0030, ADR-0026, and `docs/architecture/engine-authority-map.md`.
> **Archived because:** This plan predates the Account Clerk implementation snapshot and is retained only for provenance.

# Account Authority Consolidation and Approved-Account Pin — rev 3

> **Status:** Future execution plan. PR #1013 ships the observation lease in
> shadow mode; this document defines the hardening, parity-gated cutover, and
> follow-on consolidation work. It replaces rev 2 of this document and records
> the conclusions of the principal-architect review published on PR #1013.

## Goal

Make routine broker-account verification automatic and invisible, make every
account-level block lead to one useful remedy surface, and close the one
confirmed account-identity safety gap without building parallel lifecycle
machinery.

## Architectural decision

**Account Authority is the name of the existing broker-account safety seam, not
a new service or wrapper class.** For one broker-reported paper or live account,
the seam is already formed by:

- `accounts/<account_id>/` durable artifacts and account events;
- the account instance registry and bot order namespaces;
- `AccountTruthRefreshLoop`, Account Truth composition, and its snapshot cache;
- the Account Observation Lease introduced by PR #1013;
- account reconciliation/recovery proofs, freeze evidence, and audited
  overrides;
- the Account Monitor triage projection and remedy actions.

Do not introduce an `AccountAuthority` pass-through class, a second observer, a
second classifier, a second account ledger, or a second remedy page. If a
proposed abstraction can be deleted without forcing complexity back into its
callers, do not build it.

The one genuinely new safety mechanism is the **Approved-Account Pin**: durable
operator approval of the exact broker-reported account that the installation is
allowed to operate. The current paper/live sentinel validates account *type* but
does not reject reconnecting to a different sibling account of the same type.

## Domain fence

This plan applies only to broker-reported trading accounts in the Python data
plane. The .NET/GraphQL `/portfolio` account domain is a Strategy Lab
research/simulation model with GUID identities. It is active, intentionally
separate, and out of scope. Do not merge, rename, or delete it while implementing
this plan.

## Existing implementation map — build nothing here

| Plan term | Existing authority |
|---|---|
| Account Authority | `accounts/<account_id>/` artifact tree plus existing observer, gates, and Account Monitor |
| Account observer | `AccountTruthRefreshLoop` + `assess_account_truth()` + `AccountReconciliationService.observe_account_truth()` |
| Account observation proof | `account_observation_lease.json`; owned exposure with an active manager is allowed |
| Account recovery proof | `account_reconciliation_receipt.json`; observation plus resolved/flat exposure or accepted override |
| Bot Account Binding | `run_ledger.json.account_id` plus append-only `instance_registry.jsonl` binding |
| Broker-reported account | `managedAccounts()` single-account readback plus paper/live sentinel |
| Attribution ledger | namespace-stamped `order_ref`, intent WAL, broker `perm_id`/`exec_id`, and Account Truth folding |
| Recovery/override evidence | `AccountRecoveryProof`, `AccountAuditedOverride`, account freeze, and `account_events.jsonl` |
| Remedy Center | promotion and route consolidation of `/broker/account-monitor` |
| Revive | start the same `strategy_instance_id`; existing cold-start reconciliation adopts its own namespace evidence |

## Resolved lifecycle semantics (C1)

The phrase “prior-crash trades are clean” was too broad. The code and authority
contract already distinguish three cases:

1. An **active** bot's attributable non-zero position is clean observation
   evidence and does not require flatness.
2. Terminal orders and historical executions from a **retired** bot remain
   attributable historical facts.
3. A current position or open order whose only manager is **retired** is
   `retired_owner_live_exposure`: attributable, but unmanaged and therefore
   `not_proven` for ordinary account trading.

This plan preserves case 3 as an account-wide default hold. Other bots may
resume only after the exact bot is revived and reconciled, broker observation
confirms operator-directed resolution, or an explicit audited and time-bounded
override permits continued activity. The hold is not evidence of a foreign
trade; the UI must name the known retired owner.

Per-namespace continuation is deferred. IBKR exposes net account positions, so
allowing other bots safely—especially on the same instrument—requires a proven
conflict-containment policy. It must not be inferred merely because the retired
namespace is known.

## Non-goals

- No `AccountAuthority` facade/class.
- No multi-account IBKR session support; continue refusing `managedAccounts()`
  lists with more than one entry until an explicit account-to-observer mapping
  exists.
- No operator-editable connection-profile store in v1. `.env` and deployment
  configuration remain the connection authority.
- No broker-neutral adapter layer. Keep vocabulary broker-neutral, implement
  IBKR directly.
- No automatic adoption by an unrelated bot.
- No account-observation state machine beyond projection of connection × lease
  × freeze/condition evidence.
- No deletion of the Strategy Lab portfolio domain or the verified-live
  lifecycle modules listed in the deletion ledger below.

---

# Execution order

## Slice 0 — Harden PR #1013 against current review findings

**Deliverable:** all six unresolved inline review threads are fixed, regression
tested, replied to, and resolved before observation-lease enforcement work.

### Task 0.1 — Make refresh outcome observation shared and generation-safe

**Files:**

- `PythonDataService/app/services/account_truth_refresh.py`
- `PythonDataService/app/services/account_reconciliation.py`
- `PythonDataService/app/main.py`
- manual callers in `app/routers/broker.py`,
  `app/routers/broker_account_truth.py`, and
  `app/routers/account_reconciliation.py`
- corresponding service/router tests

**Required behavior:**

- Capture the owner-generation fence **before** registry/freeze context and
  broker Account Truth collection begin; carry that attempt context to the
  success observer.
- Compare the pre-collection fence with the post-collection fence before lease
  renewal. A changed or non-accepting generation revokes with
  `ACCOUNT_OWNER_GENERATION_CHANGED`.
- Route success and failure through one shared refresh-outcome hook used by the
  15-second loop and every manual `refresh_account_truth_now()` caller.
- A failed manual broker sweep revokes the durable lease immediately, rather
  than waiting for the loop.
- Invoke keyword-only failure callbacks with keyword arguments.
- If the second freeze read is unreadable, revoke with a bounded, explicit
  account-proof reason. Do not leave a prior VERIFIED lease passable.
- Keep consumer-side owner-generation validation in
  `assess_account_observation_lease()`; it is necessary defense in depth.

**Tests:**

- owner changes after attempt capture but before the success observer → revoke;
- stable accepting generation → renew;
- loop failure and each manual refresh failure → immediate revoke;
- keyword-only callback is actually invoked;
- malformed/unreadable freeze evidence → revoke, never preserve VERIFIED.

- [ ] Implement and verify Task 0.1.

### Task 0.2 — Require real submit-boundary identity in parity evidence

**Files:**

- `PythonDataService/app/services/observation_lease_parity.py`
- `PythonDataService/tests/services/test_observation_lease_parity.py`

Reject comparison rows lacking non-empty `strategy_instance_id` or `run_id`, in
addition to the existing gate-id/source/status validation. Malformed look-alike
events must not count toward the three-session cutover.

- [ ] Add failing missing/blank identity cases, implement, and verify.

### Task 0.3 — Bound observation copy without losing Account Monitor triage

**Files:**

- `PythonDataService/app/services/account_reconciliation.py`
- `PythonDataService/tests/services/test_account_reconciliation.py`

Normalize every current/history `reason_line` before constructing the
512-character-capped schemas. Preserve a deterministic truncation marker and
keep raw forensic detail in durable evidence/logs; only operator presentation is
bounded.

- [ ] Add oversized broker/freeze/revocation reason tests, implement, and verify.

### Task 0.4 — Review-thread closure

After tests pass and the fixes are pushed:

- reply to each of the six inline threads with the fix and test receipt;
- resolve only threads actually satisfied by the pushed commit;
- refetch thread state and leave no actionable unresolved thread silently open.

- [ ] Reply, resolve, and re-audit PR #1013 threads.

### Slice 0 verification

- `ruff check app/ tests/`
- focused affected service/router suites
- the CI fast Python baseline/change-driven suite
- existing focused Angular specs if any wire copy/schema changes

---

## Evidence Gate — complete the observation-lease shadow proof

No start/submit cutover is permitted until the existing spine plan's parity
gate succeeds:

- at least three distinct canonical NYSE paper-market sessions;
- real submit-boundary rows with instance/run identity;
- no truth=`block` / lease=`pass` comparison;
- malformed rows rejected;
- the artifact-backed parity report archived with the work.

- [ ] Collect three-session paper evidence and archive the passing report.

If the lease is ever weaker, fix and restart the evidence window; do not waive
the divergence.

---

## Slice 1 — Atomic observation-lease cutover and seamless Start

**Prerequisite:** Evidence Gate passed.

This slice is the remainder of Slices 3–4 in
`2026-07-13-reconciliation-spine-session-lease.md`; do not create a competing
implementation plan.

**Deliverable:** Start and submit consume the durable observation proof, the
redundant raw in-process gate retires only after parity, and healthy Start never
requires navigation to manual reconciliation.

### Tasks

- Add the observation-lease gate to deploy preflight and `_assert_start_allowed`.
- Preserve run-scoped cold-start reconciliation before the bar loop.
- Replace the raw submit-time Account Truth provider only in the same atomic
  cutover; retain freeze, registry, session authority, and child-local broker
  connection gates.
- On Start with an absent/expired lease, trigger one immediate run of the
  existing observer rather than waiting for the next ≤15-second cadence.
- Render **Verifying account** while that direct attempt is running; continue
  Start immediately on success.
- Keep ON_DUTY presence semantics separate from trading permission and update
  ADR-0026/authority documentation as specified in the spine plan.

- [ ] Complete the parity-gated atomic cutover.
- [ ] Complete the seamless Start pass-through UX.

---

## Slice 2 — Approved-Account Pin (the only new safety mechanism)

**Prerequisite:** Slice 1 complete. Ship as a focused follow-on PR.

**Deliverable:** reconnecting IBKR to a different account of the same paper/live
type cannot silently redirect observation or trading.

### Task 2.1 — Durable pin artifact and assessment

Create one installation-scoped artifact, separate from any individual account
directory, for example:

`accounts/approved_account_binding.json`

Minimum immutable schema:

```text
schema_version
broker = "ibkr"
account_id
mode = "paper" | "live"
connection_profile_fingerprint
approved_by
approved_at_ms
```

The fingerprint covers canonical non-secret effective connection identity
(broker, mode, normalized host, and port). Never persist credentials. Client-id
diagnostics remain visible separately and need not invalidate the account pin
unless the approved profile contract deliberately includes them.

Use existing atomic artifact I/O and strict account-id normalization. Add a
fail-closed read assessment for absent, malformed, mode-mismatched, and
account-mismatched pins.

- [ ] Implement artifact/repository/assessment with malformed and mismatch tests.

### Task 2.2 — Enforce broker readback against the pin

At `IbkrClient.connect()` after `managedAccounts()` returns exactly one account
and after the existing paper/live sentinel:

- compare the broker-reported account and effective profile with the pin;
- on mismatch, record safe diagnostic evidence, disconnect, and raise a typed
  approved-account mismatch error;
- do not set `connected_account`, renew observation proof, or permit submit;
- expose the pinned and observed account identities to broker health/diagnose
  without exposing secrets.

Every observation-lease consumer takes its expected account identity from the
approved pin and still verifies expected account == lease account == run binding.

Regression tests:

- same pinned paper account reconnects;
- sibling paper account is refused despite passing the DU-prefix sentinel;
- paper/live mismatch remains refused by the existing sentinel;
- missing/malformed pin fails closed for trading and is diagnosable;
- mismatch cannot renew or consume a lease from either account tree.

- [ ] Implement connect/readback and consumer enforcement.

### Task 2.3 — Initial approval and safe pin change

Initial migration is explicit and fail-closed: when no pin exists, the broker
may be observed for configuration diagnosis, but new trading remains blocked
until the operator approves the broker-reported account. Existing environment
values prefill the display; they are not account-identity proof.

Changing an existing pin requires durable evidence that the old authority has:

- no DEPLOYED/ACTIVE bot binding;
- no active freeze or unresolved recovery condition;
- a fresh observation and the recovery/flatness proof required by the switch
  policy.

Record the approval/change event with evidence references, then advance the old
account owner generation out of accepting and establish/advance the new account
generation. This invalidates in-flight proof. Never accept a free-typed account
identifier; the candidate must come from broker readback.

- [ ] Implement initial approval and pin-change guards with race tests.

---

## Slice 3 — Promote Account Monitor into the Account Remedy Center

**Deliverable:** every account-scope block leads to one account-specific page
that states the failed fact and offers the applicable action in place.

### Task 3.1 — Collapse account cure routing

- Promote `/broker/account-monitor`; do not create a new route/component.
- Repoint `cross-client-execution`, `live-trade-reconciliation`, and other
  account-scope runbook/cure destinations to `/broker/account-monitor` with the
  appropriate fragment.
- Route account-scoped bot/deploy blockers to the same page.
- Keep `/broker/session-mirror` for daemon/socket infrastructure.
- Remove `/broker/reconciliation`; its comparison rows have no engine-side
  values, and it must not remain a second account-evidence surface. Account
  proof and recovery live on the Account Desk.
- Replace stale “Open the Orders page…” flatten prose with the existing in-page
  `flattenExposureFromDialog()` action.

- [ ] Add route/copy regression tests and collapse the routing fan-out.

### Task 3.2 — Surface the five outcomes as a projection

Do not build a classifier. Project existing evidence into:

| Operator outcome | Existing derivation | Default effect |
|---|---|---|
| Actively attributable | bot owner + ACTIVE manager | VERIFIED; normal operation |
| Recovery-required | bot/mixed-known owner + only RETIRED manager | account-wide hold; revive/resolve/override |
| Acknowledged manual | manual owner + recorded decision/override | apply recorded policy and expiry |
| Unattributed | foreign/unclaimed or unexplained mixed residue | account-wide hold |
| Unobservable/invalid | stale, unavailable, source-stale, malformed, or account mismatch reason | account-wide hold |

Operator-visible top-level state remains a projection, not a new machine:

- **VERIFIED** — quiet proof line;
- **ATTENTION** — one backend-authored reason and one action;
- **FROZEN** — durable recovery flow.

- [ ] Implement only missing projection/copy and pin with surface tests.

### Task 3.3 — Make existing recovery verbs prominent

- “Revive” means Start the same strategy instance and run existing cold-start
  reconciliation/adoption; add no recovery daemon or handoff service.
- Show the exact retired bot/run and affected broker facts.
- Show active `AccountAuditedOverride` decision, scope, approver, and expiry.
- Verify override expiry at every consuming gate read; never cache authority
  past `valid_until_ms`.
- Keep flatten-requested distinct from broker-confirmed flatness.

- [ ] Add recovery/override UX and expiry characterization tests.

---

## Slice 4 — Account Configuration v1 and IBKR setup documentation

**Deliverable:** operators can see the effective IBKR configuration, diagnose
it, approve the connected account pin, and follow product-owned setup guidance.

### Task 4.1 — Read-only effective configuration and pin UI

Build on existing broker health/diagnose endpoints. The page displays only safe
effective values:

- paper/live mode;
- normalized host and port;
- data-plane client ID and live-runner client-ID pool;
- broker-reported account and paper/live sentinel result;
- approved-account pin status and mismatch reason;
- existing eight-check broker diagnosis.

Warn when the data-plane client ID overlaps the live-runner pool. Do not render
credentials or add a mutable connection-profile database. The only write action
is explicit approval/change of the broker-reported account pin through the
guarded Slice 2 flow.

- [ ] Implement the read model, page, approval action, and overlap tests.

### Task 4.2 — Product-owned IBKR setup guide

Document the concrete Gateway/TWS configuration required by this application:

- paper/live Gateway selection and matching ports;
- enabling API/socket clients and any required active API policy;
- read-only versus order-capable mode;
- client-ID separation and runner pool requirements;
- container/host addressing used by the deployment;
- diagnosis steps for connection, sentinel, account-pin, and client-ID errors.

Link the relevant official IBKR documentation, but keep the app guide complete
enough to operate without reconstructing our requirements from external pages.
Verify current official URLs during implementation.

- [ ] Publish and link the setup guide from Account Configuration.

---

# Deletion and retention ledger

## Delete or consolidate in the owning slice

- Remove the never-authored `"intended"` exposure-resolution value from
  `AccountExposureResolution` and `CLEARABLE_EXPOSURE_RESOLUTIONS`; retain a
  regression test that unsupported values cannot clear a freeze.
- Delete stale manual-flatten prose after the in-page action is the routed cure.
- Consolidate `_clear_freeze_blocker()` and `clear_account_freeze()` onto one
  shared validation authority; the artifact writer remains the final guard.
- Retire the raw in-process Account Truth submit gate only after the parity-gated
  Slice 1 cutover.
- Re-evaluate the auto-reconciliation policy toggle and child-run private truth
  sweeps only in later, separate decisions after production evidence.

## Do not delete

- `app/engine/live/reconciliation_receipt.py`;
- `app/services/resume_guard_state.py`;
- `app/services/operator_blockage_ladder.py`;
- `Backend/GraphQL/PortfolioQuery.cs` and the Strategy Lab `/portfolio` domain;
- daemon/process liveness lease, run reconciliation receipt, freeze evidence,
  account registry, intent WAL, or consumer-side generation checks.

---

# Completion criteria

The future program is complete only when:

- all PR #1013 review threads are addressed and CI is green;
- three-session shadow parity is archived with no lease-weaker outcome;
- start and submit consume the observation lease without weakening existing
  gates;
- a healthy bot starts without manual reconciliation navigation;
- reconnecting to a sibling paper/live account is refused by the approved pin;
- no bot exists without an immutable broker-account binding;
- every account-level block routes to Account Monitor/Remedy Center with a
  precise fact and usable cure;
- account configuration is diagnosable and documented without creating a
  mutable secret/configuration store;
- the deletion ledger is executed without removing verified-live machinery;
- authority docs, CONTEXT vocabulary, backend contracts, frontend fixtures, and
  tests agree on active-owned versus retired-unmanaged exposure.

## New-session pickup

1. Work on branch `codex/bot-startup-prereqs` / PR #1013.
2. Fetch unresolved review threads with the thread-aware GitHub review script.
3. Start at Slice 0; do not implement the approved pin or lease enforcement
   before the stated prerequisites.
4. Preserve unrelated `compose.yaml` work in the main workspace.
5. After Slice 0, re-check CI and paper-session parity state before choosing the
   next executable slice.
