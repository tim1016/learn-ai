# Bot Control & Account Clerk — Operator Manual

> # AUTHORITY STATUS: CANONICAL — SOLE CURRENT AUTHORITY
>
> **Canonical file:** `docs/bot-control-operator-manual.md`.
>
> This is the sole current operating and implementation-reference document for Bot
> Control and Account Clerk behavior. Humans, coding agents, reviewers, and UI
> maintainers must use this document instead of older plans, runbooks, audits,
> handoffs, or implementation snapshots. The in-app page at `/broker/bot-manual`
> renders the synchronized copy of this file.
>
> **Verified against:** `bcbfe935a07139023f045aae277078058368857c` (HEAD),
> 2026-07-29. **Audience:** traders, operators, reviewers, and coding agents.
> **Surfaces governed:** the IBKR Bot fleet and detail pages (`/broker/bots`,
> `/broker/bots/:id`), Account Desk and Account Safety (`/broker/accounts/:accountId`),
> the rendered manual, and their FastAPI control/read boundaries. It also records the
> boundary to the separate Alpaca Broker System v2 desk (`/brokers/alpaca`); it does
> not misrepresent that desk as the IBKR host-daemon control plane.
>
> This supersedes former operator runbooks, AccountOwner snapshots, cockpit/cohort
> guides, controller plans, quick procedures, and point-in-time audits as present
> operating instructions. Current ADRs remain the separate authority for durable
> decisions and invariants: ADR-0001–0008, ADR-0010–0019, ADR-0022, ADR-0024–0027,
> ADR-0029–0033. ADR-0028 is proposal-only except for its explicit surviving channel
> contract. `docs/known-gaps.md` owns open defects, not implemented behavior.
>
> When this document conflicts with executable code, committed contracts, or focused
> tests, treat this document as defective: verify the implementation, correct this
> document, and record any unresolved ADR conflict. Do not follow the conflicting
> older document.

**Audience:** operators running paper-trading bots on the learn-ai live stack.
**Scope:** how to deploy, run, stop, and recover bots; how the Account Clerk keeps an
account honest; every gate that can block you; and the non-obvious blindspots that
bite in practice.

---

## 0. Authority and implementation snapshot (2026-07-29)

This is the sole current operating manual. The **Trader** view is not a
separate, competing manual: it renders the same backend-authored lifecycle,
account, and action facts that this manual describes. Angular does not derive a
trading verdict from raw data.

| Concern | Current authority | Implementation boundary |
|---|---|---|
| Normal account broker writes and account journal | **Account Clerk** | `app/engine/live/account_clerk.py`, `account_clerk_operations.py`, `account_clerk_rpc.py` |
| Admitted-order custody and safety composition | **Clerk journal and typed safety artifacts** | `account_custody_projection.py`, `account_epoch.py`, `account_safety.py`, `services/account_safety_snapshot.py` |
| Duty phase, roster, durable desired state | **Lifecycle evaluator** | `app/engine/live/bot_lifecycle_evaluator.py` |
| Bot process actuation and observed process facts | **Host live-run daemon** | `app/engine/live/host_daemon.py` |
| Start and submit account proof | **Account Truth** today | `app/services/account_gate_promotion.py`; the `observation_lease` branch is shadow-only until its promotion evidence exists |
| Cockpit and Trader presentation | **Python-authored surface; Angular renders it** | `app/services/operator_surface.py`, `app/services/account_cockpit.py`, and `Frontend/src/app/components/broker/` |
| Concurrent starts | **Individual fresh roll-call offers; repeat-churn guard per bot** | `app/routers/live_instances.py`, `account_artifacts.py`; the cohort launcher is retired |

For an operator, the result is simple: act from Bot Control or Account Desk,
read the backend-authored reason and receipt, and do not use an archived guide,
script, or alternate UI as a broker-control path. The separate Alpaca desk has its
own broker-neutral contract and Clerk; it is not a second route into this IBKR
control plane.

## 1. Mental model — three planes

Everything in this system is one of three planes. Most confusion comes from
attributing a failure to the wrong plane.

1. **Broker connection (the launch daemon).** A single host process, the
   **host live-run daemon** (`:8765`), is the sole authority that binds
   `strategy_instance_id → run_id` and owns the bot subprocesses. Deploy/start/stop
   all route through it. IB Gateway (`:4002` paper) is the broker socket behind it.
2. **Account hygiene (the Clerk).** One **Account Clerk** process per account is the
   *normal* account broker-write authority, and it keeps a
   durable, append-only journal of everything. "Is this account clean/flat/verified?"
   is the Clerk's plane.
3. **Bot lifecycle (the roster).** Each bot has a durable phase
   (OFF_DUTY / ON_DUTY / RETIRED) plus a daily duty roster and roll-call. "Is this bot
   on duty, resting, sick, or decommissioned?" is the lifecycle plane.

**The most important synthesis:** *lifecycle and connection fail; hygiene reports it.*
A critical hygiene verdict (contamination, freeze, not-proven) is usually a **symptom**
whose root cause lives in one of the other two planes. When you see one, ask "which
plane is this telling me about?" before touching the Clerk.

---

## 2. Runtime topology

The stack straddles **containers** (data/UI plane) and **host processes** (the live
control plane + IB Gateway). This split is the #1 source of "why doesn't this work"
surprises — read §12.2.

| Component | Port | Runs in | Responsibility |
|---|---|---|---|
| **Data plane** (`polygon-data-service`, FastAPI) | `:8000` | Container | Market data + the live-runs/broker router. Fronts the daemon over HTTP; serves the operator UI's control endpoints. `compose.yaml:75-79` |
| **Host live-run daemon** (`host_daemon.py`) | `:8765` | **Host process** | Sole `strategy_instance_id→run_id` authority (ADR 0004). Mints runs (git clean-tree check), spawns/signals subprocesses, supervises the Clerks. `host_daemon.py:2214` |
| **Account Clerk** (per account) | Unix socket (no TCP) | **Host subprocess** of the daemon | Account-scoped broker-write authority + the journal. Socket under `<tmpdir>/learn-ai-clerk/<sha256>.sock`. `account_clerk.py:725-734` |
| **IB Gateway** | `:4002` paper (`4001` live, `7497`/`7496` TWS) | **Host** (external app) | The IBKR brokerage socket. `broker/ibkr/config.py:66` |
| Backend (.NET) | `:5000`→`:8080` | Container | GraphQL API. `compose.yaml:236` |
| Frontend (Angular) | `:4200` | Container | Operator SPA. `compose.yaml:302` |
| Postgres / Redis | `:5432` / `:6379` | Container | DB / cache. `compose.yaml:12,40` |

All ports bind loopback (`127.0.0.1`) by default. The daemon's `:8765` is **not** a
compose port — it's a host process the container reaches via
`host.containers.internal` (`compose.yaml:193-195`).

### 2.1 Two secrets guard two seams
- **`X-Data-Plane-Control-Secret`** (env `DATA_PLANE_CONTROL_SECRET`) — guards every
  mutating data-plane route (and some sensitive reads). **503** if unset (fail-closed),
  **403** on mismatch. `data_plane_control.py:44-57`. Dev fallback
  `local-dev-control-secret` (`compose.yaml:135`) — must never reach a shared host.
- **`X-Live-Runner-Token`** (env `LIVE_RUNNER_DAEMON_TOKEN`) — guards every daemon
  route. If env is unset, the daemon generates one at startup and writes it to
  `<artifacts_root>/.host-daemon-token` (mode `0600`), a sibling of `live_runs/` that
  the container sees through the artifacts bind mount. The data plane reads env-first,
  then that file. **401** on mismatch; there is no open mode. `daemon_auth.py:40-113`.

---

## 3. The Account Clerk

### 3.1 What it is
One process per account, the **normal** account broker-write authority. Every bot reaches
the normal broker-write lane by submitting an intent to the Clerk over RPC (the bot-side
client never holds the normal account writer, `account_clerk_rpc.py:115-116`).
The Clerk serializes all intake behind one append-only journal
(`clerk_journal.jsonl`), which is the single source of truth for that account's
exposure. It refuses to run or write unless the broker is in **paper** mode
(`ACCOUNT_CLERK_PAPER_MODE_REQUIRED`, `account_clerk.py:528-532,778-779`).

Single-writer is enforced three ways: a host-wide authority file lock held for the
Clerk's whole lifetime (`account_clerk.py:706-722`), an in-process serial intake lock,
and **generation fencing** on every broker write (a superseded Clerk raises
`AccountClerkGenerationFencedError` and cannot write, `account_clerk.py:613-664`).

### 3.2 Generations & the two leases (know the difference)
- **Generation** — a monotonically increasing integer identifying the current Clerk
  incarnation. Bumps **once, on spawn** (`clerk_generation.json`); adopting an
  already-running Clerk does **not** bump. A generation record alone is *not* authority
  — it requires a matching, unexpired `RUNNING` lease (`account_artifacts.py:911-951`).
- **Clerk lease** (`clerk_lease.json`) — "is the Clerk process alive right now?" TTL
  **5 s**, renewed ~1 Hz by the Clerk. Carries `pid`, `ibkr_client_id`, `status`
  (`RUNNING`/`DRAINING`), `valid_until_ms`. `account_clerk_lease.py:11`.
- **Observation lease** (`account_observation_lease.json`) — "is the account's exposure
  verified clean *and still tied to this Clerk*?" `VERIFIED`/`REVOKED`, TTL = Account
  Truth readiness TTL, carries a `truth_watermark`. **It revokes on any Clerk
  generation change** (`ACCOUNT_CLERK_GENERATION_CHANGED`) or a dirty/stale sweep, and
  would be consulted only under the `observation_lease` authority.
  `account_observation_lease.py:166-210`.

**Current gate selection:** `account_truth` is the effective start and submit authority.
`observation_lease` remains a Clerk-fenced shadow comparison until its promotion
criteria are met; do not treat a present lease as permission to start.

> **Blindspot:** a Clerk restart bumps the generation → the observation lease revokes →
> starts can be blocked with `ACCOUNT_OBSERVATION_LEASE_REVOKED` until you reconcile.
> A fresh reconcile re-verifies the lease against the new generation.

### 3.3 Attachment & operating state (what the status endpoint means)
`GET /api/accounts/{account_id}/clerk` returns a backend-authored projection — do not
re-derive it on the client:
- **attachment**: `ATTACHED` (generation == lease generation, lease `RUNNING`, not
  expired) / `UNATTACHED` (never spawned) / `FENCED` (superseded or lease
  gone). `account_directory.py:251-265`.
- **phase**: `accepting` / `reconnecting` / `draining` / `frozen`. Only `accepting` is
  usable by RPC/adoption.
- **operating_state**: `READY` ("N bots on duty") / `STANDBY` ("Ready — no bots on
  duty") / `ATTENTION` ("needs attention" — not attached).
- **journal**: `{last_seq, last_write_ms}` watermark.

### 3.4 The journal
Strict append-only JSONL with contiguous `seq` from 1; any gap/blank/bad row raises
`AccountClerkJournalCorruptError`. A separate `clerk_inbox.jsonl` is the crash-boundary
WAL: an accepted intent is replayed on restart so it can't be silently lost. Entry
kinds you'll care about:
- **`recorded` (intent)** — durable receipt of a bot's submit intent (unique on
  `intent_id` and `order_ref`).
- **`broker_event`** — one deduplicated IBKR callback (keyed by exec_id, *not* seq).
  A callback with **no** durable intent is recorded as an account fact but triggers the
  unattributed-event guardrail: an `exposure` freeze with
  `RECONCILE_UNATTRIBUTED_BROKER_EVENT` (`account_clerk.py:413-455`).
- **`operator_adjustment`** — an immutable **journal cure** (§6.3).

#### Corrupt journal: the one operator-required exception

`ACCOUNT_CLERK_JOURNAL_CORRUPT` blocks **every** Clerk broker-write boundary.
The Account Desk is the only recovery surface: confirm `QUARANTINE` to durably
rename the corrupt journal **and its paired crash-boundary inbox** aside forever,
then confirm `REBASELINE` to take a fresh broker snapshot and seed a new journal.
Neither original artifact is deleted or truncated. Every holding discovered at re-baseline is retained as
broker-evidence-only flag-and-hold exposure; no bot namespace is guessed and
admission remains frozen until normal reconciliation proves ownership. There
is deliberately no terminal/filesystem procedure and no fallback broker writer.

**Exposure is projected two ways from this one journal** (`journal_exposure.py`), and
the difference matters enormously (§5.2):
- **Per-instance / namespace fold** (`group_by="strategy_instance"`) counts only fills
  that carry a Clerk **intent** — it never invents an owner for unattributed flow.
- **Account-truth fold** counts **all** fills including unattributed ones.

### 3.5 Custody is a receipt, not an order acknowledgement
For normal IBKR paper strategy entries, the current Clerk path is asynchronous custody:
the bot returns after **A0**, the fsynced Clerk admission receipt, while the Clerk-owned
worker performs the later broker work. A0 is neither an IBKR order ID nor proof that an
order was accepted by the broker. The later facts are A1 (broker write started), A2
(broker identity correlated), and A3 (economic terminal outcome). The Account Desk
transaction detail is the read surface for those facts; it preserves unknown facts and
their separate source, arrival, and durable-record clocks.

The qualified production topology permits eight account-wide normal-entry slots and
zero asynchronous risk-reducing slots. A ninth normal entry is a typed refusal before
A0. A caller timeout around A0 is **unknown**, not a refusal or permission to retry:
read the exact custody record or replay the exact identity. A normal entry remains
limited to one nonterminal intent per `(account_id, strategy_instance_id)`.

Risk-reducing work must not be smuggled through this entry lane. In particular, native
IBKR stock execution has no externally reachable atomic reduce-only primitive, so a
server-derived exact close remains blocked while the account is suspended. Presented
Account Safety actions and the Clerk decide whether any recovery action is available.
Angular only displays those backend-authored facts and actions.

### 3.6 Socket topology (why the data plane delegates cures)
The Clerk RPC socket lives at
`tempfile.gettempdir()/learn-ai-clerk/<sha256(artifacts_root + "\0" + account_id)>[:32].sock`
(`account_clerk.py:725-734`). It's on the **host** `/tmp`. A containerized data plane
has a *different* `/tmp` namespace (not bind-mounted) **and** computes a different hash
(container artifacts path `/app/artifacts` ≠ host path). The operator UI does not open
that socket: its journal-cure route delegates to the authenticated host daemon, which
performs the host-local Clerk RPC. Deploy/start/stop use the same daemon boundary.
`SOCKET_MISSING` through the UI is therefore a host-delegation failure to escalate, not
an instruction for the operator to run a direct socket command.

### 3.7 Managing the Clerk
- Status: `GET /api/accounts/{account_id}/clerk`.
- Ensure/start (idempotent, adopts a healthy Clerk or spawns one, blocks on a
  generation handshake): `POST` daemon `…/clerk/ensure` — the data-plane reconcile /
  cure / flatten endpoints call this for you first.
- Release on detach: daemon `…/clerk/release`.
- The Clerk is spawned with `start_new_session=True`, so it **outlives the daemon**;
  on daemon boot the supervisor *adopts* a still-healthy Clerk rather than double-
  spawning (it verifies the RPC-served generation matches the lease before trusting it).

---

## 4. Account hygiene & reconciliation

### 4.1 Account Truth + the reconciliation receipt
`POST /api/accounts/{account_id}/reconciliation` ensures the Clerk, sweeps the broker,
and writes a durable receipt. The receipt has exactly **two** states
(`account_reconciliation.py:19`):
- **`CLEAN`** — all of: connected account matches, truth matches + fresh,
  `final_verdict=="clean"` with no critical source-freshness block, and
  `exposure_resolution ∈ {flat, accepted_override}`.
- **`NOT_PROVEN`** — anything else.

Key fields: `exposure_resolution` (`flat` only if every position **and** symbol-exposure
row is zero), `positions`, `owner_summaries` (each with `owner_binding_state`
DEPLOYED/ACTIVE/RETIRED/UNKNOWN and `gross_position_quantity`), and `source_freshness`
(a **critical** stale source blocks CLEAN even if the verdict is clean — you can't
receipt over stale broker evidence). Receipt TTL is 5 min, and a **CLEAN receipt is
auto-invalidated** if a new broker execution lands after it was written.

Read-only variants: `GET …/reconciliation/latest` (cached, no sweep),
`GET …/triage` (the full recovery projection the UI renders).

### 4.2 Fleet contamination (ADR 0005) and the "two truths"
`GET /api/live-instances/account-summary?account_id=…` returns the fleet verdict:
```
residual[symbol] = net_broker[symbol] − Σ explained_by_instance[symbol]
```
- `net_broker` comes from the **live IBKR position snapshot** (only after the broker
  proves the account id matches).
- `explained_by_instance` comes from the **Clerk-journal per-instance fold** (intent-
  attributed only).
- verdict = `clean` (no residual) / `contaminated` (residual) / `unknown` (broker
  unavailable — never guessed). `policy_blocks_starts` = true only when contaminated
  (or unknown) under the policy gate. `fleet.py:17-80`.

> **This is the single most important hygiene concept.** The receipt's cleanliness and
> the fleet "explained" sum come from **two independently-derived truths** — the broker
> snapshot vs. the journal fold. When they disagree, `residual ≠ 0` → contaminated.
> A stale journal claim or an unattributed fill both surface here. See §5.2/§6.

### 4.3 clean ≠ flat
"Clean" (broker snapshot flat) is **not** the same as "flat" (every managed claim
reconciled to zero). If a bot **exits holding a position and its closing fill is never
journaled**, the per-instance fold still shows the open → the fleet residual goes
non-zero even though the broker is flat, and the namespace shows
`owner_binding_state=RETIRED` with a non-zero `gross_position_quantity`. This is the
recording gap that a **journal cure** fixes (§6.3). *(Observed live 2026-07-17: two
validation bots ended holding +1 with no journaled close → residual SPY -1/QQQ -1 blocked
new starts while the receipt read CLEAN.)*

---

## 5. The bot lifecycle

### 5.1 Three layers — do not conflate
1. **Durable phase** `BotLifecyclePhase` (`bot_lifecycle_state.py:31`):
   `OFF_DUTY` (on roster, idle) / `ON_DUTY` (a process is live) / `RETIRED` (terminal;
   needs a fresh deploy to reopen).
2. **Display status** `BotDisplayStatus` (7 values, precedence RETIRED → ON_DUTY/
   CLOCKING_OUT → **SICK_BAY** → OFF_ROSTER → READY → OFF_DUTY): the closed UI
   vocabulary. `READY` = on roster + a live roll-call offer. `SICK_BAY` = ≥1 open
   condition (blocked until cured). `OFF_ROSTER` = excluded from tomorrow's duty.
3. **Process state** (transient, from the daemon): `running` / `stopping` / `exited` /
   `idle` / `unreachable`. `unreachable` ≠ `idle` — the former means the daemon
   couldn't be queried at all.

### 5.2 Roll-call & the offer gate
`POST /api/live-instances/roll-call` mints a **single-use start offer** for each
*eligible* bot (phase OFF_DUTY, on roster, not sick-bay, start-capable). An offer
expires at the session's `effective_stop_ms` and is consumed on start. **Every start
needs a fresh offer** — the start endpoint re-checks it server-side, so a stale
"ready" projection can't arm a start. Run roll-call each session.

Summary counts: `ready` (offers minted) / `on_duty` (ON_DUTY + CLOCKING_OUT) /
`off_duty` / `sick_bay` / `off_roster` / `retired`.

### 5.3 The operations
| Op | Endpoint | Notes |
|---|---|---|
| Deploy | `POST /api/live-instances` (201; 200 idempotent) | Needs `strategy_spec_path`, `qc_audit_copy_path`, `qc_cloud_backtest_id`, `strategy_key`, `live_config` (**must** have `sizing`), `start_date_ms`, `strategy_instance_id`, `start:bool`. Forwards to the daemon (git clean-tree check). |
| Deploy-preflight | `GET /api/live-instances/deploy-preflight?strategy_key&instance_id&account_id` | Returns `{ready, blockers[]}`. |
| Start | `POST /api/live-instances/runs/{run_id}/start` | Start requires a matching fresh roll-call offer. In Bot Control, choose **Read-only** for no broker orders or **Paper orders** to enable paper execution; **Live orders** is visibly blocked. |
| Stop bot gracefully | Lifecycle desired-state action | Writes a durable `STOPPED` intent and asks the bound host process to exit. It does **not** flatten and requires **Resume** before any later start. **Free — not rate-gated.** |
| End day now | `POST /api/live-instances/{sid}/end-day-now` | Queues Clerk-owned `CLOCK_OUT`; it becomes OFF_DUTY only after durable clean-exit/account evidence. This is the re-offerable session-close path. |
| Roster toggle | `POST /api/live-instances/{sid}/lifecycle/roster` | `on_roster:bool`. |
| Retire & Replace | `POST /api/live-instances/{sid}/retire-and-replace` | **`confirm_account_flat:true` required**; refuses while running/stopping/unreachable. The cure path for a crashed bot. |
| Delete (soft) | `DELETE /api/live-instances/{sid}` | Hides from surfaces, preserves artifacts for audit. Redeploy under a new run to un-hide. |
| Catalog | `GET /api/live-instances/catalog` | `{bots, roll_call, evening_report}`. |

### 5.4 End day vs. Stop vs. halt/crash — the fork that decides your morning
This is the single most operationally important distinction.

- **End day now** → Clerk-owned clean exit → phase **OFF_DUTY** only after flat broker
  proof and a durable receipt → the bot is **re-offered** by the next roll-call. Use
  this for a normal session close.
- **Stop bot gracefully** → durable **`STOPPED`** intent plus a request for the bound
  process to exit. It does not place orders or flatten. A later start is refused with
  `STOPPED_REQUIRES_RESUME` until you choose **Resume**, which restores `RUNNING`; then
  take a fresh roll-call offer and start normally.
- **Halt / crash** (process exits *not while stopping*, or nonzero/unproven exit) →
  the daemon writes a `RETIRED` binding with a recovery-required source
  (`process_crashed` / `ended_without_status` / `boot_liveness_unproven`). That
  (a) hard-fails the next start/deploy with **`CRASH_RECOVERY_REQUIRED`** and
  (b) forces **SICK_BAY**, which makes the bot roll-call-**ineligible**. Cure: verify
  the account is flat with no open orders and either record an audited recovery
  override **or** use **Retire & Replace**.

### 5.5 Exit taxonomy (`exit_taxonomy.py:10`)
`clean` (normal/force_flat) · `controlled_stop` (operator stopped) · `halted`
(fatal_halt / max_orders — *not* recovery-blocking) · **`crashed`** (recovery-blocking)
· **`ended_without_status`** (outcome unproven — recovery-blocking) · `poisoned`
(separate poison gate) · `recovery_flatten` · `interrupted`. A separate
`boot_liveness_unproven` source blocks restart for a bot that never proved liveness.

---

## 6. The admission gates (why a deploy/start is blocked)

Gates run at three seams: **deploy** (`deploy_instance`), **start**
(`_assert_start_allowed`), and **per-bar submit** (running bot). Deploy-*without*-start
only hits the host deploy gates + account-freeze + fleet/broker-truth; crash-recovery,
coherence, preflight, and start-boundary are gated on `start=true`.

### 6.1 Deploy-time (host daemon, run for every deploy)
| reason / error | HTTP | Trigger | Clear |
|---|---|---|---|
| `DirtyTreeError` | 409 | git tree dirty within scope `['PythonDataService','references/qc-shadow']` | Commit/stash in-scope changes. Untracked files *outside* scope are fine. |
| `SpecOrAuditMissingError` | 400 | spec or audit-copy path missing on disk | Fix paths. |
| `SizingPolicyMissingError` (VCR-0001) | 400 | `live_config` has no `sizing` | Add `{"sizing":{"kind":"FixedShares","value":1}}`. |
| `ActionPlanReadinessError` (`ACTION_PLAN_EMPTY`, `…_ENTRY_LEG_REQUIRED`, …) | 400 | `deployment_validation` needs 1 long stock entry + a matching close leg | Supply a valid action plan. |
| `StrategyInstanceIdAlreadyUsedError` | 409 | bot name already owns a run | New name, or redeploy as a child (`parent_run_id`). |
| `RunAlreadyExistsError` | 409 | true collision (idempotent re-deploy → 200 `created=false`) | Usually benign. |
| `GitUnavailableError` / `DeployIOError` | 503 | git/filesystem problem | Repair environment. |

### 6.2 Deploy + start
| reason_code | HTTP | Trigger | Clear |
|---|---|---|---|
| `ACCOUNT_FROZEN` | 409 | a freeze flag exists (`unresolved_exposure.flag`) | Recovery: reconcile + clear-freeze, or accept-override (§6.5). |
| `FLEET_CONTAMINATED` | 409 | non-zero fleet residual | Clear via recovery — cure the stale claim or flatten (§6.3). |
| `BROKER_TRUTH_UNAVAILABLE` / `BROKER_ACCOUNT_MISMATCH` | 409 | broker account unknown/unreadable, or ≠ run's account | Wait for a fresh broker sweep / connect the right account. |
| `CRASH_RECOVERY_REQUIRED` (gate `account.crash_recovery`) | 409 | previous run crashed with no later recovery proof | Verify flat + no open orders, record an audited override (valid 15 min) or Retire & Replace. |
| `IDENTITY_COHERENCE_UNCONFIRMED` / `EXPOSURE_COHERENCE_UNCONFIRMED` | 409 | inherited symbol/exposure disagrees and no confirmation submitted | Submit the matching confirmation, or deploy without start. |
| `DEPLOY_PREFLIGHT_BLOCKED` (carries `blockers[]`) | 409 | a blocking preflight condition (daemon_down, broker_disconnected, account_frozen, account_not_proven, fleet_contaminated, strategy_not_validated, instance_already_running) | Resolve the first blocker's remediation. |
| `NO_TRADING_SESSION` / `SESSION_STOP_REACHED` (gate `daily_lifecycle.effective_stop`) | 409 | no NYSE session, or now ≥ the bot's effective stop (force-flat time) | Start on a session day, before the effective stop. |

### 6.3 Start-endpoint-only
| reason_code | HTTP | Trigger | Clear |
|---|---|---|---|
| `ROLL_CALL_OFFER_REQUIRED` / `_EXPIRED` / `_STALE` / `_RUN_MISMATCH` | 409 | start needs a fresh, matching roll-call offer | Run roll-call, Start from the current offer before it expires. |
| `BOT_RETIRED` / `BOT_LIFECYCLE_STATE_UNREADABLE` | 409 | phase RETIRED / corrupt state | Deploy a replacement / repair state. |
| `BOT_SOFT_DELETED` | 410 | deletion marker present | Redeploy under a new run. |
| `ACCOUNT_OBSERVATION_LEASE_ABSENT/_EXPIRED/_REVOKED`, `ACCOUNT_CLERK_GENERATION_CHANGED` | 409 | *(only under `observation_lease` authority)* lease not VERIFIED | Reconcile now — a clean sweep re-verifies the lease. |
| `STOPPED_REQUIRES_RESUME` | 409 | A prior graceful Stop left the durable STOPPED latch | Choose Resume, then obtain a fresh roll-call offer and start normally. |
| `REDEPLOY_REQUIRED` / failing `poison_sentinel` | 409 | A fatal halt or explicit Mark poisoned wrote `poisoned.flag` | Retire & Replace / deploy a fresh run; Resume cannot reuse the poisoned run. |
| `HOST_SERVICE_OFFLINE` | 409 | daemon has no process view | Preserve the backend receipt and escalate to the platform owner; Bot Control has no host-restart bypass. |
| `ALREADY_RUNNING` / `STOPPING` | 409 | already live/shutting down | Wait. |
| `START_SETTINGS_INCOMPLETE` | 409 | ledger `strategy_key` can't hydrate a Start | Ensure a valid strategy key in the ledger. |

### 6.4 Restart-intensity (the repeated-activation ceiling) — READ THIS
`RestartIntensityPolicy` is **3 activations within a 5-minute rolling window for the
same `strategy_instance_id`**, measured under the account's durable binding ledger
(`account_artifacts.py::_restart_intensity_groups`). The third activation of that one
bot freezes the account (`restart_intensity.threshold_breached`, then
`ACCOUNT_FROZEN`). It is neither a fleet-concurrency cap nor a limit on one first start
per distinct newly deployed bot. Eight distinct first deployments do not aggregate into
one restart storm; repeatedly stopping and reactivating the same bot does.

Every actual start still needs its own fresh roll-call offer and all ordinary admission
proofs. Stops are free (only accepted activations count). Start sequentially whenever
you need to observe evidence between launches; that is an operational practice, not a
false claim that the third distinct bot is prohibited.

The freeze is **transient/auto-expiring** (the window rolls off), and — since
2026-07-17 — a *running* bot **pauses submits and survives** a restart-intensity freeze
rather than halting (`TransientAccountFreezePauseError`, §7). Clearing it early: a fresh
clean reconcile + `freeze/clear` resets the window.

### 6.5 Per-bar submit gates (a *running* bot refusing to submit)
Evaluated per bar in `submit_pending_orders`. Pending orders are always dropped
*before* the decision, so nothing is ever submitted while blocked.
- **`AccountFreezeBlockError`** (durable freeze) → **halt**.
- **`TransientAccountFreezePauseError`** (restart-intensity freeze) → **pause this bar,
  keep running, resume on clear** (the fix validated live 2026-07-17).
- **`SessionPolicyBlockError`** (outside allowed sessions / missing extended-hours ref
  price) → pause this bar.
- **`AccountTruthBlockError`** → halt (with a **120 s** grace window for a transient
  truth-outage on a durable broker before it halts).
- **`AccountRegistryBlockError`**, **`BrokerSafetyVerdictBlockError`** (a non-`paper-only`
  verdict — e.g. a LIVE account detected mid-session), **`SubmitUncertainHaltError`**,
  **`LiveBrokerEventStreamError`** → halt.
- **`MaxOrdersPerDayExceeded`** (ledger default **2000**) → halt; resets on the session
  boundary.

---

## 7. Freezes & recovery

### 7.1 Freeze anatomy
A freeze is the durable `unresolved_exposure.flag` (`AccountFreezeEvidence`). Two axes:
- **transient vs. durable:** transient = **restart-intensity** (reason starts
  `restart_intensity.threshold_breached`; auto-expires). Durable = everything else
  (exposure/contamination/reconciliation/unhealthy-clerk) and needs operator action.
  *The transient-vs-durable split is driven by the reason prefix, not by `freeze_kind`.*
- **`freeze_kind`** = `account` vs `exposure` (independent axis; governs which clear
  path applies).

`read_account_freeze` returns `None` once `cleared_at_ms` is set — a cleared freeze
reads as absent. A live freeze projects a `FROZEN` triage verdict.

### 7.2 Clearing a freeze
- **Durable freeze → `POST /api/accounts/{account_id}/freeze/clear`.** Requires a
  fresh, newer-than-the-freeze, **CLEAN**, non-invalidated, unexpired receipt; for
  exposure freezes also `exposure_resolution ∈ {flat, accepted_override}`. On success
  it records an `account_recovery_proof`. **So the sequence is always: reconcile →
  (get CLEAN) → clear-freeze.**
- **Exposure freeze you can't get flat → `POST …/freeze/accept-exposure-override`.**
  Writes an audited override ("I accept this exposure is real and mine";
  `cleared_source=account_audited_override`).
- **Transient restart-intensity → clears itself** (window rolls off); or force it with
  reconcile + `freeze/clear`.

### 7.3 The three flatten/cure remedies — pick the right one
| Situation | Remedy | Places broker orders? |
|---|---|---|
| Broker **holds** positions, no surviving run, no exact candidate | **Snapshot-bound emergency Flatten** — Account Safety action panel | Yes — the signed action rechecks current evidence, requires **`FLATTEN`**, and never reports the account flat without a fresh reconciliation receipt. It is suppressed when an exact candidate exists. |
| One retired namespace, one instrument, broker **still holds** it, journal-proven exact order | **Snapshot-bound recovery Flatten** — Account Safety action panel | Yes — one exact server-authored order, bound to the current namespace, contract ID, and signed quantity. The raw recovery endpoint is retired. |
| Broker **flat** but a retired namespace's journal still claims a position | **Journal cure** — `POST /api/accounts/{id}/journal-cures` | **No** — appends an immutable compensating adjustment. |

**Journal cure specifics** (the clean≠flat fix): preview with
`GET …/journal-cures/preview?bot_order_namespace=…&symbol=…`. The namespace must be a
**proven-RETIRED** binding, the cure may only **reduce toward zero** (never cross zero
or overshoot), and it's idempotent on `idempotency_key`. It corrects the *ledger's
memory*, not the *account* — never use it when the broker actually holds the position.
Use the Account Desk; its authenticated route delegates the host-local Clerk operation
for you.

---

## 8. Concurrency — recipes

**Start several bots safely (while avoiding repeated-activation churn):**
1. Ensure the tree is clean in scope, the account is CLEAN/flat, fleet clean, no freeze.
2. Deploy all N (`start:false`) — deploys don't count toward the rate limit.
3. `roll-call` → get offers.
4. Start one bot and verify its On duty and account evidence.
5. Verify each bot's backend-authored On duty and account evidence before continuing.
   Never batch or stagger starts through a cohort launcher; it has been removed.

**Stop-and-replace (churn):** use **End day now** for a clean/re-offerable session
close, then start a replacement (which counts toward the rate limit). A deliberate
**Stop bot gracefully** needs Resume before that bot can start again; crashed bots need
Retire & Replace.

**Submit vs observe:** Bot Control labels the choice clearly: **Read-only** runs without
broker orders, **Paper orders** enables paper execution, and **Live orders** is blocked.

---

## 9. Common operator procedures

**Recover a frozen account:**
`reconcile` → confirm `CLEAN`/flat → `freeze/clear` → confirm `freeze_banner:null`.
(If exposure is real and you accept it: `accept-exposure-override`.)

**Cure a stale claim (clean≠flat):**
confirm broker flat (`reconcile` positions 0) → preview the cure for the retired
`(namespace, symbol)` → apply `signed_quantity` reducing toward zero, citing the CLEAN
receipt id in `evidence_refs` → re-check fleet `clean`. Use the Account Desk; it
delegates the host-local Clerk operation.

**Recover a crashed (sick-bay) bot:**
verify account flat + no open orders → **Retire & Replace** (`confirm_account_flat:true`)
→ deploy the replacement.

**Host service unavailable after a code change:** Bot Control and Account Desk do not
offer a host restart or bypass. Preserve the surfaced receipt and escalate to the
platform owner; verify refreshed daemon/Clerk evidence before restarting a bot.

---

## 10. Terminal incident procedures

The Activity tab is stream-first. When a terminal row appears, expand it before taking
action: the row and its deduplicated incident are one visible story, carrying the
authored summary, identity ladder (`evaluation_id`, `intent_id`, `order_ref`, `req_id`,
`order_id`, `perm_id`, `exec_id`), gate walk, and terminal evidence. Do not create a
parallel ad-hoc checklist for the same failure.

<!-- terminal-runbook-slug: ibkr-order-rejection -->
### IBKR order rejection

Triggered by `order.rejected` / `order_rejected`.

1. Capture the IBKR `external_code`, `external_message`, `req_id`, and `order_ref`.
2. Verify in IBKR / TWS whether the order is absent, cancelled, or still working. Do
   not retry from the bot until the broker state is known.
3. If the broker message identifies a fix (buying power, contract qualification,
   account restriction, order attribute), resolve that cause and redeploy or resume
   only after Bot Control shows the required readiness gates are clear.
4. If the wording is unmapped or not actionable, preserve the evidence and escalate
   the classifier gap; do not invent operator copy in the UI.

<!-- terminal-runbook-slug: submit-outcome-uncertain -->
### Submit outcome uncertain

Triggered by `submit.uncertain` / `submit_uncertain`.

1. Assume the order may exist until proven otherwise.
2. Use the row identity (`order_ref`, `req_id`, `order_id`, `perm_id`) to verify order
   status in IBKR / TWS.
3. Do not submit a replacement until the original order is confirmed absent, cancelled,
   or reconciled into the bot's state.
4. Preserve the incident evidence if manual reconciliation is required; it records why
   the bot refused to continue.

<!-- terminal-runbook-slug: bot-halted -->
### Bot halted

Triggered by `submit.halted` / `halted`.

1. Inspect the blocking gate or fatal halt trigger in the expanded row.
2. Preserve any surfaced `halt.flag` evidence. A halt caused by broker safety, Account
   Truth, Clerk fencing, or submit uncertainty must not be manually cleared.
3. Resolve the underlying gate, then redeploy or resume only when the backend-authored
   cockpit verdict agrees.

<!-- terminal-runbook-slug: bot-launch-failed -->
### Bot launch failed

Triggered by `submit.launch_failed` / `launch_failed`.

1. Inspect the terminal error source (`daemon`, `os`, or `broker_session`) in the row.
2. For daemon or OS failures, preserve the redacted log path and stderr tail from the
   evidence and escalate to the platform owner.
3. For broker-session failures, resolve the concrete connection cause (for example, a
   client-id collision or account-sentinel refusal) before redeploying.
4. Prefer redeploy after the cause is fixed; do not hand-edit run artifacts or event
   journals.

<!-- terminal-runbook-slug: unmapped-terminal-diagnostic -->
### Unmapped terminal diagnostic

Triggered by `submit.unmapped_diagnostic` / `unmapped_diagnostic`.

1. Treat the row as a classifier gap, not an operator-fixable condition.
2. Preserve the stream-drawer evidence and the run logs.
3. Add or correct the backend classifier/template so the same failure becomes a mapped
   terminal outcome with specific operator copy.

---

## 11. Troubleshooting quick reference

| Symptom | Likely cause | Fix |
|---|---|---|
| Start → `409 ACCOUNT_FROZEN` after repeatedly restarting the same bot | Restart-intensity (3 activations/5 min for one bot) tripped | Wait ~5 min or reconcile + clear-freeze; do not churn the same bot. |
| Start → `ROLL_CALL_OFFER_REQUIRED` | No fresh offer this session | Run `roll-call`, start from the offer. |
| Deploy → `DirtyTreeError` naming a path you don't recognize | Stray file in scope (often test-artifact junk like `PythonDataService/PythonDataService/`) | Remove it; commit/stash real changes. |
| Cure → `ACCOUNT_CLERK_UNAVAILABLE:SOCKET_MISSING` | Host-daemon delegation to the host-local Clerk failed | Retry from Account Desk only after the backend advises it; otherwise preserve the receipt and escalate. |
| Fleet `contaminated` but receipt `CLEAN` | clean≠flat: unjournaled close (stale per-instance claim) | Journal cure the retired namespace. |
| Bot "running" but never trades | Strategy entry conditions unmet, or **Read-only** was selected | Check its effective posture; use **Paper orders** only when paper execution is intended. |
| Bot vanished from roll-call after a crash | Went to sick_bay (recovery-required) | Retire & Replace. |
| Start → `ACCOUNT_OBSERVATION_LEASE_REVOKED` | Only possible when the observation-lease authority has been promoted | Reconcile and follow the backend-authored recovery; Account Truth is the effective authority today. |
| Deploy/start works but cure/status flaky | Host-daemon delegation or Clerk health is degraded | Read the Account Desk posture and escalate if it cannot offer a restore path. |
| "It's the wrong code" | Host services do not hot-reload automatically | Escalate to the platform owner; re-observe daemon and Clerk evidence before restarting a bot. |

---

## 12. Blindspots — the things that bite

**12.1 Read-only vs. Paper orders is an explicit launch choice.** **Read-only** runs the
strategy without broker orders; **Paper orders** enables paper execution; **Live orders**
is displayed but blocked. A bot can run all day and place zero orders when Read-only was
selected. **Check effective posture before waiting for fills.**

**12.2 Host vs. container is a boundary, not an operator fork.** The data plane and UI
delegate deploy/start/stop and Clerk-only recovery work to the authenticated host daemon.
The Clerk socket remains host-local; the UI never opens it directly. If that delegation
is unhealthy, Account Desk reports the condition and the operator escalates rather than
trying a second control path.

**12.3 clean ≠ flat.** A CLEAN receipt only says the broker snapshot is flat. A bot that
exited holding a position whose close never journaled leaves a stale per-instance claim
→ fleet `contaminated` → new starts blocked. The receipt and the fleet "explained" sum
are two different truths (broker snapshot vs. journal fold). Cure the ledger; don't
"re-flatten" a position the broker doesn't hold.

**12.4 A transient freeze used to kill healthy bots; now it pauses them.** Before the
2026-07-17 fix, a restart-intensity freeze cascade-**halted** every running bot. Now
they **pause submits and survive**. The freeze still blocks new starts, but it is
triggered by repeated activation of the same bot (three within five minutes), not by a
fleet of distinct first starts.

**12.5 A Clerk restart invalidates shadow lease proof.** Generation bumps revoke the
observation lease. That matters only after its promotion; **Account Truth remains the
effective authority today**. In either case, re-observe the Account Desk before a new
start after a Clerk bounce.

**12.6 End day, Stop, and crash are different.** **End day now** is the normal
session-close path: after Clerk-owned clean-exit proof it becomes OFF_DUTY and can be
re-offered. **Stop bot gracefully** writes a STOPPED latch and later needs **Resume**.
A crash leads to sick bay plus `CRASH_RECOVERY_REQUIRED`; the bot will not roll-call
until recovery evidence is recorded or you use Retire & Replace. Do not use emergency
flatten for a normal day end.

**12.7 Roll-call offers are per-session and single-use.** A start needs a fresh offer;
they expire at the session's `effective_stop`. A stale "Ready" chip won't start a bot —
run roll-call.

**12.8 Host services do not hot-reload.** The running daemon/Clerk evidence, not the
working tree, proves what is live. A host-service restart is a platform-owner operation;
after one, re-observe the Account Desk before admitting a bot.

**12.9 A dirty-tree deploy block is an engineering handoff.** Unexpected generated or
nested files can trip the deploy clean-tree gate. Do not bypass the gate; hand the
reported path to the platform owner and retry only after a clean preflight.

**12.10 Emergency flatten is blunt and account-wide.** It market-closes *every* position
and is suppressed when an exact per-namespace recovery candidate exists. Prefer the
surgical **snapshot-bound recovery Flatten** when a candidate is offered. Both are
operator-confirmed broker actions and must be submitted from the Account Safety action panel: the server validates
the signed action, typed `FLATTEN` confirmation, current proof, and post-action reconciliation
before it reports any position or account flat.

**12.11 Unattributed broker events freeze the account.** A fill callback with no Clerk
intent (manual order, foreign flow, an out-of-band tool) is recorded but triggers an
`exposure` freeze (`RECONCILE_UNATTRIBUTED_BROKER_EVENT`). Don't place orders on a
Clerk-owned account outside the Clerk.

**12.12 Time is `int64 ms UTC` everywhere.** Every timestamp on the wire/at rest is
integer ms UTC. Don't compare or render raw values in feature code — render through the
shared timestamp component with an explicit mode (`local`/`et`/`date-et`). Session
structure comes from the canonical calendar, never hardcoded `09:30`/`16:00`.

**12.13 IBKR client-id pool is finite.** Bots/Clerks draw from a fixed client-id pool
(50–99); a second competing IBKR connection (e.g. a stray host data plane alongside the
container) can exhaust subscriptions (error 322). Run **one** data plane.

**12.14 The restart-intensity threshold is hard-coded.** `threshold=3`,
`window=300_000ms` are not env-configurable (`account_artifacts.py:221`). To change the
churn envelope you change code + ship it; don't expect a knob.

---

## 13. Glossary
- **Daemon** — the single host process owning bot subprocesses and run bindings (`:8765`).
- **Clerk** — the per-account, single-writer broker authority + journal.
- **Generation** — integer fencing a Clerk incarnation; bumps on spawn.
- **Clerk lease** — 5 s liveness lease for the Clerk process.
- **Observation lease** — durable Clerk-fenced proof used only by the currently dormant
  promotion branch; Account Truth is effective today.
- **Roll-call offer** — a single-use, session-scoped permit to start one bot.
- **Fleet residual** — `net_broker − Σ journal-explained`; non-zero = contaminated.
- **clean ≠ flat** — broker snapshot flat but a journal claim still open.
- **Journal cure** — an append-only adjustment that reconciles a stale claim to broker
  truth (no orders).
- **Effective stop** — the force-flat time; you can't start after it.
- **Sick bay** — a bot with an open condition (usually crash-recovery-required),
  ineligible for roll-call until cured.

---

## 14. Engineering appendix — implementation truth matrix (verified 2026-07-29)

This appendix is for maintainers and reviewers. It is not a second operator procedure.
Status describes the implementation at the verified revision, not design intent.

| Claim / boundary | Production owner and registration | Contract / focused evidence | Human consumer | Status |
|---|---|---|---|---|
| IBKR Bot Control runtime | `host_daemon.py`, registered through `live_instances.py` and `main.py` | `/api/live-instances/*`; host-daemon tests | `/broker/bots`, `/broker/bots/:id` | current |
| IBKR normal account writes | `account_clerk.py` plus RPC and journal modules; daemon supervises one Clerk/account | Clerk RPC schemas; Clerk/journal tests | Account Desk, Bot Control custody ribbon | current |
| Lifecycle phase and control intent | `bot_lifecycle_evaluator.py`; router commands are requests, daemon reports facts | `LiveInstanceStatus`, lifecycle tests | Bot fleet and detail | current |
| Account safety composition | `account_safety_snapshot.py`; source artifacts retain their own clocks | `AccountSafetySnapshot`, snapshot tests | Account Desk and Bot Control | current |
| Custody A0→A3 | Clerk journal + `account_custody_projection.py`; strategy uses `submit_custody_v2` | transaction projection and custody qualification tests | Account Desk transaction history | current, eight-entry / zero-risk-reducing topology |
| Reconciliation and contamination | `account_reconciliation.py`, `fleet.py`, journal exposure folds | reconciliation and `test_journal_exposure.py` | Account Desk, start admission | current |
| Account recovery actions | presented-action services and Clerk/daemon delegation | presented-action schemas and recovery tests | Account Safety / Account Desk | current; no Angular verdict synthesis |
| Trader/operator wording | `operator_surface.py`, operator guidance, presented action services | operator-surface and frontend surface tests | trader and operations lenses | current; backend authors judgment/prose |
| Raw receipt identifiers | shared `receiptLabel` pipe at presentation boundary | pipe/component tests and frontend guards | all evidence surfaces | current; opaque IDs/refs remain exact |
| IBKR activity narrative | host-runner callback WAL then broker-activity projector | activity schemas and publisher tests | Bot Control Activity | current; projector is a rebuildable projection |
| Alpaca Broker System v2 reads/orders | broker contract registry → Alpaca adapter/Clerk, registered in `main.py` | `/api/brokers/alpaca/*`, committed OpenAPI, adapter/Clerk tests | `/brokers/alpaca` | current but separate from IBKR Bot Control |
| Alpaca bot runner | `services/bot_runner.py`, `broker_bots.py` | `/api/brokers/{broker}/bots/*`, `test_bot_runner.py` | no equivalent Bot Control UI route | current but log-only; daemon-free and no order submission |
| Legacy lifecycle projection | deleted runtime/routes/client wrappers; Python lifecycle-evidence fold remains | retirement guards and migration tests | none | retired runtime; opaque historical template IDs are not a revival |
| Observation lease | `account_observation_lease.py` and promotion service | gate-promotion tests | Account Desk evidence | shadow-only; Account Truth is effective admission authority |

### 14.1 Broker-specific boundary

The phrase “Bot Control” in this manual primarily names the established IBKR
host-daemon control plane. Alpaca is paper-only Broker System v2: its read and order
routes are broker-neutral contract routes under `/api/brokers/alpaca`, its desk is
`/brokers/alpaca`, and its daemon-free bot runner is presently log-only. Neither route
is evidence that an Alpaca order may use the IBKR Clerk, roll-call, host daemon,
reconciliation, or emergency-recovery controls. Do not generalize an IBKR-specific
socket, client-ID, Account Truth, or recovery rule to Alpaca without a contract change.

### 14.2 Current code-versus-ADR conflicts

1. **ADR-0026 has stale superseded prose inside its accepted record.** Its original
   §§3 and 5 say Pause/Resume/STOPPED are deleted, while the explicit 2026-07-21
   amendment says `PAUSED`, `RUNNING`/Resume, and `STOPPED` remain durable commands;
   current lifecycle and frontend code implement the amendment. Documentation/UI may
   safely follow the amendment. The ADR needs an owner-approved consolidation; do not
   revive the original “one start path — resume is deleted” language.
2. **ADR-0033's initial synchronous-baseline text is amended by its own A0 cutover.**
   The current strategy path uses `submit_custody_v2` and returns at A0; the initial
   paragraph remains historical baseline context. The current code and “Strategy A0
   cutover amendment” govern operation.
3. **Crash-recovery admission is not closed under deploy-only staging.** Current code
   blocks direct restart after `crashed`, `boot_liveness_unproven`, or
   `ended_without_status`, but a later same-instance `start=false` deployment can
   replace the latest binding before Start checks it. This is BUG-16 in the 2026-07-28
   audit and remains an implementation safety conflict, not a documentation defect.

### 14.3 ADR-to-machinery traceability and disposition

| ADR family | Current machinery / reachability | Disposition |
|---|---|---|
| 0001, 0004, 0006, 0007 | file-backed artifacts, host daemon, durable bindings, daemon token, data-plane forwarding | essential safety complexity; retain |
| 0002, 0005, 0009, 0011, 0021–0023, 0029 | adapter paper guard, Python readiness/sizing/session evidence, deploy posture | retain; several are IBKR-specific and must not be claimed for Alpaca |
| 0008, 0010, 0013–0017, 0024–0027 | Clerk journal, durable intent/action receipts, authored surfaces/narratives/notices, lifecycle evaluator | essential product and safety complexity; retain |
| 0018, 0019 | read-only session mirror and composed daemon diagnostics | essential observability; keep deferred items in `known-gaps.md` |
| 0020 | strategy-validation selection design | proposed decision; verify an implementation claim before presenting it as a gate |
| 0028 | proposed channel contract; Clerk/lifecycle authority explicitly superseded | preserve as proposal only; do not treat roadmap machinery as live authority |
| 0030 | account-rooted Clerk, journal authority, legacy-name/read compatibility, lifecycle-projection deletion ledger | retain authority; qualify each compatibility reader before removal |
| 0031 | FastAPI/OpenAPI snapshot and generated frontend contracts | essential boundary discipline; retain |
| 0032 | broker-neutral contract, verbatim capture, Alpaca adapter/Clerk | active migration/product complexity; retain; IBKR v1 adapter remains unbuilt |
| 0033 | A0–A3, epoch/safety composition, producer logs, qualification campaign | essential safety complexity; retain; reconcile its amendments into an ADR revision |

### 14.4 Prioritized cleanup and simplification register

| Priority | Mechanism / originating decision | Evidence and consumers | Preserve before change | Required proof and disposition |
|---|---|---|---|---|
| P0 | Same-instance deploy-only crash-recovery bypass (BUG-16; lifecycle binding model) | Latest-binding lookup can mask a blocking retired binding; deploy/start admission and Bot Control consume it | A blocking terminal outcome must prevent restart until a recovery proof or replacement exists | Regression for all three blocking sources through deploy `start=false` then Start; preserve historic binding readability and rollback. **Safety-critical; standalone fix PR.** |
| P1 | Legacy `AccountOwner` names/readers (ADR-0030 compatibility ledger) | Historic wire/event/model names remain in Clerk-backed paths and artifact readers | Historic journals/events must remain readable and must never regain writer authority | Inventory persisted artifacts and dual-read consumers; migration fixture + cold-start/recovery tests; then consolidate or remove. **High risk.** |
| P2 | Legacy `account_events.jsonl` repair/read compatibility (ADR-0033) | Historical forensic reader and repair endpoint remain after producer-log split | Pre-split evidence stays inspectable; forward writers never return | Prove no writer, migration-age/retention decision, corrupt-file recovery test; then deprecate reader/repair endpoint. **Medium risk.** |
| P3 | Duplicate manual presentation paths | Markdown source is bundled and rendered twice (compiled article and optional raw Markdown) | One canonical source must remain byte-synchronized and accessible | Existing sync guard plus route/component test; keep both render modes only if accessibility/audit needs justify them. **Low risk; documentation/UI cleanup PR.** |
| P4 | Observation-lease shadow branch (ADR-0030) | Shadow comparisons and history are retained while Account Truth gates admission | Promotion must never weaken account proof or let stale generation evidence admit a start | Promotion qualification evidence or explicit retirement plan, shadow-parity tests, artifact migration/readback test. **High risk; do not delete speculatively.** |
| P5 | IBKR/Alpaca parallel control concepts | Separate host-daemon/Clerk and Broker v2 paths have intentionally different capabilities | Do not erase broker-specific safety, custody, or recovery semantics | Complete capability matrix and prove equivalent invariants before sharing code; otherwise retain clear separation. **High risk; architectural ADR required for consolidation.** |

The apparent fan-out in the Clerk/daemon/journal path is mostly essential: it spans a
single writer, durable artifacts, RPC/host boundary, recovery, and proof surfaces.
The removal candidates above are prioritized only after their persisted-data and
recovery qualifications, not because the implementation is large.

---

*Maintainers: keep this in lockstep with the code. If you touch a gate reason code, a
lifecycle phase, a lease TTL, an endpoint path, or a freeze/cure rule, update the
matching section here and the citation. Drift makes an operator manual worse than none.*
