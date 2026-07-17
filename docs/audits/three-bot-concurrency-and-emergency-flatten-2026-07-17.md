# Three-bot concurrency + guarded emergency-flatten review — 2026-07-17

Investigator: Claude (autonomous session, handoff from Codex).
Scope: (1) review commit `d5e05b9f` (guarded account emergency flatten);
(2) root-cause and remedy the residual stale-exposure defect on
`DUM284968`; (3) investigate readiness for three concurrent bots plus a
dynamic add/stop/start churn.
Account: `DUM284968` (paper). Start state: `master` @ `d5e05b9f`, worktree clean.

---

## 0. Executive verdict

| Question | Verdict |
|---|---|
| Is commit `d5e05b9f` safe to ship? | **Yes, with two low-severity follow-ups** (server does not verify the `FLATTEN` token; the mutation endpoint's paper fence is transitive, not independent). |
| Was the residual stale-exposure defect real? | **Yes.** Fleet contamination was live-blocking starts (`FLEET_CONTAMINATED`, `policy_blocks_starts=true`) while the reconciliation receipt was `CLEAN`. |
| Is journal cure the correct remedy? | **Yes.** Applied two append-only cures (SPY −1, QQQ −1); fleet is now `clean`, `policy_blocks_starts=false`. |
| Can we run three concurrent bots? | **Proven yes** — the morning cohort ran 3 bots with a **59.0-minute** genuine 3-way overlap and ~52 interleaved fills. Independently validated from the durable Clerk journal. |
| Did the afternoon churn (4th bot / stop 3rd / start 5th) run? | **No — blocked by data-plane topology** (see §4). The deploy path is not drivable in the current container-only data-plane. Procedure + fix documented. |

---

## 1. Commit `d5e05b9f` — guarded account emergency flatten

Reviewed all nine requested dimensions. Files: `routers/account_reconciliation.py`,
`services/account_reconciliation.py`, `engine/live/host_daemon.py`,
`engine/live/host_daemon_client.py`, `engine/live/run.py` (`cmd_emergency_flatten`),
`schemas/live_runs.py`, and the Account-Desk recovery frontend.

### 1.1 Mutation-boundary safety — **PASS**
Three independent layers, each re-checking:
- **Data-plane** `POST /api/accounts/{id}/emergency-flatten`: canonicalizes id;
  `400` unless `confirm=true`; `400` unless `request.account == path`; **re-runs
  `triage()` and returns `409 ACCOUNT_EMERGENCY_FLATTEN_NOT_DECLARED` if
  `emergency_flatten_confirmation is None`.**
- **Daemon** `POST /accounts/{id}/emergency-flatten` (`dependencies=auth`):
  re-validates `confirm` + `account==path`; runs the CLI under a per-account
  exclusion fence (`_flatten_lock` + `_flatten_in_flight`) → a concurrent flatten
  for the same account is rejected `409`, preventing double-liquidation against
  the same pre-fill snapshot.
- **CLI** (`cmd_emergency_flatten`): exit `2` without `--confirm`; `--account`
  must match the connected account; makes its own broker calls (independent of a
  poisoned LiveEngine).

### 1.2 Paper/live fencing — **PASS, one defense-in-depth note**
- Data-plane: `_emergency_flatten_confirmation()` returns `None` unless
  `receipt.account_truth.account.is_paper` (plus fresh, non-expired, non-invalidated
  evidence, connected-account match, non-zero broker positions, and **no exact
  retired-bot recovery candidate**). A live account therefore never gets the
  confirmation → the mutation endpoint `409`s.
- CLI: refuses a non-paper account via the `IbkrClient` connect-time `DU`-account
  sentinel + the `place_paper_order` invariant.
- **Note (low severity):** the mutation endpoint has **no independent
  `if not is_paper: raise`** — its live fence is *transitive* through the triage
  recheck. The CLI `DU`-sentinel is the last backstop. If
  `_emergency_flatten_confirmation` were ever widened to declare for live, the
  endpoint would forward the order and only the CLI would stop it. Recommend an
  explicit `is_paper` assert at the mutation boundary.

### 1.3 TOCTOU (triage ↔ mutation) — **PASS, elegant**
- The endpoint re-runs `triage()` at mutation time (not just on the read that
  advertised the button). A residual window remains between that recheck and the
  CLI submit, but the **CLI reads live positions at flatten time and liquidates
  current reality**, so there is no stale-snapshot risk; the per-account fence
  prevents concurrent double-liquidation.
- **Natural idempotency via broker truth:** the gate keys on non-zero positions.
  A second call after a *successful* flatten finds the account flat →
  `confirmation=None` → `409 NOT_DECLARED` (blocks double-flatten). A second call
  after a *failed* flatten finds positions still open → re-declares → allows a
  retry. The gate is self-idempotent.

### 1.4 Account/run audit ownership + temp-run leak — **PASS, one edge to pin**
- The daemon mints `eflat-<uuid>` as the audit run; the CLI writes
  `emergency_flatten.log` + `emergency_flatten_audit.jsonl` there and registers an
  `AccountInstanceBinding` (`ACTIVE`→`RETIRED`) + `register_emergency_flatten`
  under a synthetic namespace via the **single** account Clerk.
- The run dir persists as an **audit artifact** (intended, not a leak); it is not
  adopted as a live bot because the binding lands `RETIRED`.
- **Edge worth a regression test:** the emergency flatten journals its liquidating
  SELLs under its own namespace *with intent*. Flattening a position opened by a
  *managed* namespace nets to residual 0 (opener +1, emergency −1). Flattening a
  **foreign/unattributed** long (opener had no Clerk intent) would leave the
  emergency namespace at −1 with nothing to net against → a transient fleet
  residual `+1`. Pin this in a test so the escape hatch can't itself manufacture a
  contamination verdict.

### 1.5 Idempotency + outcome-unknown — **PASS**
- `HostDaemonOutcomeUnknownError` → `_outcome_unknown_http_error` (honest "unknown";
  operator must reconcile). No completion event is appended on that path.
- On success, `append_account_event(..., only_if_receipt_absent=True)` keyed by
  `account-emergency-flatten:{audit_run_id}` makes the completion event idempotent.

### 1.6 Frontend rolling-version fail-closed — **PASS, one contract note**
- `emergency_flatten_confirmation: OperatorConfirmationCopy | null` is a **new**
  field. An older backend omits it → parsed as `null` → button hidden. **Fail-closed.**
- The store generalizes Account Desk from "untokenized confirmations only" to
  tokened confirmations: `canConfirm` now requires `providedToken === requiredToken`,
  so the operator must type `FLATTEN`.
- **Note (low severity):** the `FLATTEN` token is enforced **only on the client**.
  `EmergencyFlattenRequest` carries just `{account, confirm}` — the server never
  verifies the token, and `broker.service.ts` posts `{account, confirm:true}`. For a
  paper account guarded by the triage recheck this is acceptable *friction*, but the
  token is **not a server contract**; a direct API caller bypasses it. Consider
  moving the token into the request + server check if it is meant to be a real gate.

### 1.7 Single canonical Clerk authority — **PASS**
Emergency flatten and journal cure both route through the one account Clerk
(`AccountClerkRpcClient` → daemon → CLI `register_emergency_flatten`). No second
writer or second journal is introduced.

### 1.8 Live gate validation
On the current **flat** account, `GET .../triage` returns
`emergency_flatten_confirmation: null` — the button is correctly **not** advertised
(no non-zero positions). The gate behaves as designed live.

---

## 2. Residual stale-exposure defect — root cause + remedy

### 2.1 Symptom
`GET /api/live-instances/account-summary?account_id=DUM284968` reported
`verdict: contaminated`, `explained_total {SPY:1, QQQ:1}`, `net_positions {}`,
`residual {SPY:-1, QQQ:-1}`, `policy_blocks_starts: true` — while the canonical
reconciliation receipt was `CLEAN`/`flat` (`positions: []`, `symbol_exposures: []`,
`operator_blockers: []`). Starting bots would have hit `409 FLEET_CONTAMINATED`.

### 2.2 Root cause (two projections, two truths)
- **Reconciliation receipt** account-truth is built from the **live broker position
  snapshot** (flat).
- **Fleet "explained"** (`services/fleet_contamination.py` →
  `project_journal_exposure(group_by="strategy_instance")`) is built from the
  **Clerk-journal fill fold**, counting only fills that carry a Clerk **intent**.
- From the journal: **cohort-a ended net +1 SPY and cohort-b ended net +1 QQQ** —
  each bot's *last journaled action was a BUY with no closing SELL* (cohort-c, by
  contrast, ended net 0). Those +1 longs were later closed **outside the Clerk's
  journaled view** (external/paper flatten), so the broker went flat while the
  journal retained the opens. Broker truth (authoritative for positions) is
  dispositive → the +1 journal claims were stale.

This is the "clean ≠ flat" class: a run that exits holding a position leaves a
journal claim that only self-heals if the closing fill is journaled.

### 2.3 The `unresolved_exposure.flag`
Already carried `cleared_at_ms` (audited override, 2026-07-16) and feeds **no**
active projection — `triage` shows `freeze_banner: null`, `operator_blockers: []`.
It is dormant/superseded; **no action needed**. (It is *not* the mechanism behind
the fleet residual — the fleet projection never reads it.)

### 2.4 Remedy applied
Journal cure is the correct remedy (append-only, immutable, preserves the audit
trail; never delete the journal). Applied via the Clerk (evidence-cited to the
`CLEAN` reconciliation receipt):

- cohort-a `learn-ai/cohort-paper-20260717-a/v1` SPY **−1** → journal seq **479**
- cohort-b `learn-ai/cohort-paper-20260717-b/v1` QQQ **−1** → journal seq **480**

Post-cure verification (canonical projection **and** live endpoint):
`verdict: clean`, `residual: {}`, `explained_total: {}`, `policy_blocks_starts: false`.
**Start blocker cleared.**

> Caveat on the remedy path: the cure had to be driven from a **host** process, not
> the container data-plane — see §4.

---

## 3. Three-concurrent-bot readiness — PROVEN

Independently reconstructed from `artifacts/accounts/DUM284968/clerk_journal.jsonl`:

| Bot | Fills | BUY/SELL | Net | Window (CT) |
|---|---|---|---|---|
| cohort-paper-20260717-a | 17 | 9/8 | +1* | 08:49:06–09:48:06 |
| cohort-paper-20260717-b | 17 | 9/8 | +1* | 08:49:06–09:48:08 |
| cohort-paper-20260717-c | 18 | 9/9 | 0  | 08:49:07–09:50:39 |

*net +1 = the stale claim cured in §2.4.

**Genuine all-three concurrent overlap: 08:49:07 → 09:48:06 CT = 59.0 minutes**,
~52 interleaved fills across three namespaces on one paper account, all
attributed by `bot_order_ref` in the one Clerk journal. Concurrency of three bots
sharing an account, with per-namespace ownership preserved end-to-end, is proven.

---

## 4. Data-plane topology finding (blocks the afternoon churn)

The daemon (`host_daemon`, pid 46264) and the account Clerk
(`account_clerk`, pid 1206, gen 37) run as **host** processes. The data-plane
currently serving `:8000` is the **container** `polygon-data-service`. That
container is **structurally unable to drive live deploys or Clerk-RPC operator
actions** against the host daemon/clerk:

1. **Clerk RPC socket unreachable.** The socket is
   `$TMPDIR/learn-ai-clerk/<sha256(artifacts_root+account)>.sock`. The host clerk
   binds `…/T/learn-ai-clerk/ebd21bb4….sock`; the container computes a *different*
   digest (`/app/artifacts` vs the host path) **and** its `/tmp` is unshared, so
   `AccountClerkRpcClient.exists()` is `False` → `ACCOUNT_CLERK_UNAVAILABLE:SOCKET_MISSING`.
   This is exactly why the journal cure had to run from a host process.
2. **Validated-strategy source not mounted.** `qc-audit-copies` `scope_root` is
   `references/qc-shadow`, but `compose.yaml` mounts only
   `PythonDataService/app`, `artifacts`, `cache`, `.git`, and a few configs —
   **not `references/`**. So the container's catalog and audit-copy listing are
   empty; there is nothing to deploy from.

What **does** work from the container: reads of `artifacts/` (reconciliation,
fleet projection) and **daemon-routed** control over HTTP (`daemon-health` returns
`ok:true`; the network path is fine, auth aside). So bot *starts* would route
container→daemon→host, but there is no deployable spec to start.

**Consequence:** the morning cohort was necessarily launched by a **host**
data-plane (which sees `references/` and shares `/tmp`). No host data-plane is
running now. Standing one up blind — with the correct secret/daemon-token/IBKR env
and without disturbing the healthy daemon/clerk the #1027 work depends on — was
judged too risky to do unattended, so the **afternoon churn (add 4th / stop 3rd /
start 5th) was not executed.** This blockage is itself the headline
operational-readiness finding.

### 4.1 Procedure to run the churn safely (next session)
1. Bring up a **host** data-plane (as on 2026-07-16), co-located with the daemon +
   clerk, so `references/qc-shadow`, the host `/tmp` clerk socket, and host
   `artifacts/` are all visible; env carries `DATA_PLANE_CONTROL_SECRET`, the
   live-runner/daemon token, and IBKR paper settings.
2. Confirm gates green (already true now): fleet `clean`, observation lease
   `VERIFIED`, clerk `ATTACHED` gen 37, `deploy-preflight` pass.
3. Deploy + start 3 bots (`POST /api/live-instances` with `start:true`, valid
   `strategy_spec_path`/`qc_audit_copy_path`/sizing `live_config`).
4. Soak ≥ 1h; watch `account-summary` stays `clean` and each namespace's exposure
   nets as expected.
5. Churn: deploy+start a 4th; `POST /runs/{id}/stop` the 3rd; deploy+start a 5th.
   Watch fleet reconciliation, the observation lease, and the exit taxonomy under
   churn. **Expect** the same "clean ≠ flat" gap if a stopped bot exits holding a
   position — that is the exact case the Account Custodian PRD (#1114) auto-cure
   would close.

---

## 5. Recommendations / open decisions

1. **Data-plane must be host-resident for live control.** Either always run the
   data-plane on the host, or add the `references/` mount **and** a shared clerk
   socket dir to the container — and have the container *refuse to advertise*
   live-control endpoints it cannot fulfill (fail-closed rather than
   `SOCKET_MISSING` at mutation time).
2. **Unify the two exposure truths.** Reconciliation-receipt account-truth (broker
   snapshot) and fleet "explained" (journal fold) disagree on a recording gap.
   Consider reconciling the fleet projection against broker-flat proof so a
   journaled-open/closed-outside gap **self-heals** instead of requiring a manual
   cure — i.e., the Account Custodian auto-cure (#1114).
3. **Emergency-flatten follow-ups (both low severity):** add an independent
   `is_paper` assert at the mutation boundary; decide whether the `FLATTEN` token
   should be a server contract (move to request + verify) or remain UI friction.
4. **Pin the emergency-flatten-of-a-foreign-long accounting** (§1.4) with a
   regression test so the escape hatch can't manufacture a contamination verdict.

---

## Appendix — key evidence commands

```
# Fleet block (before) → clean (after cure)
GET  /api/live-instances/account-summary?account_id=DUM284968
POST /api/accounts/DUM284968/journal-cures  (SPY -1 seq 479, QQQ -1 seq 480)

# Concurrency reconstruction
project_journal_exposure / normalize_journal_broker_event over
artifacts/accounts/DUM284968/clerk_journal.jsonl (478→480 entries)

# Topology
account_clerk_socket_path(host_root) exists=True ; (/app/artifacts) exists=False
compose.yaml volumes: no references/ mount
```
