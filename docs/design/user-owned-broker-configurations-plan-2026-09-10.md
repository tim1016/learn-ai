# User-owned broker configurations — delegation plan

**Status:** Proposed implementation plan, amended 2026-09-10 with the owner decisions in section 0; no runtime changes authorized or performed by this document.
**Requested outcome:** Save named user configurations in the database, keep API keys and secrets outside the database, and let the operator select the saved configuration to use.
**Scope assumption:** Alpaca Broker V2 first, with an application-wide classification rule. Other user-facing environment settings receive an inventory and follow-up scope, not a simultaneous migration.
**Inspection baseline:** `08c55fda`, branched from `codex/shadow-live-review-followup` on 2026-09-10. Plan branch: `codex/user-config-profile-plan`; worktree: `/Users/inkant/learn-ai-worktrees/user-config-profile-plan`.

## 0. Owner decisions (2026-09-10)

A blindspot review against the code at the baseline produced five decisions. They override any conflicting text below, and sections 1–8 are amended to match.

| # | Decision | What it rules out |
| --- | --- | --- |
| D1 | **Scope:** v1 covers everything, including the six live envelope values. | A paper-only first phase. |
| D2 | **Storage:** a dedicated SQLite profiles database on the Clerk volume (`learn-ai-alpaca-clerk-data`), beside custody and the arming ledger. | Postgres. `my-postgres` is expendable: the README's full reset (`podman compose down -v`) deletes `pgdata` but keeps the external Clerk volume. ADR 0001 and ADR 0035 §6 keep Postgres out of the live control plane, and `python-service` has no `depends_on: db`. |
| D3 | **Loosening:** the browser may stage any live value; it takes effect on entries only at the CLI re-arm. | A browser change that reaches live entries or exits by itself. |
| D4 | **Workers:** one worker in v1, with no worker identity. Each credential slot is injected into exactly one worker. | Multi-worker selection. A Paper twin run before v2 is its own installation. |
| D5 | **Switching:** a staged selection takes effect only when the operator presses Apply. A crash or automatic restart boots the last-effective revision, and so does a refused Apply. | "Next restart applies", and any path that leaves a worker with no broker while positions are open. |

These obligations follow. Each is a work item, not an option (paths under `PythonDataService/`):

- **Loss hold judged against the sealed envelope (D3).** Today the clear calls `sync.observe()` (`app/services/alpaca_live_envelope.py:98`), which computes the limit from the configured values (`app/broker/alpaca/clerk/sqlite/live_envelope_sync.py:284`). Raising a limit and restarting therefore releases a same-day hold. Prerequisite 0a.
- **Exits price from the sealed envelope (D3).** `xh_exit_bps` is captured when the authority is composed (`app/broker/alpaca/clerk/program_leg.py:46, 197`) and EXIT is never envelope-gated, so a staged exit-pricing change would otherwise bypass the re-arm. Exits price from the newest *armed* record's sealed envelope (disarm records carry none) and fall back to the effective revision only when no armed record is readable. An exit is never blocked for lack of a seal.
- **The arming plan shows a before→after diff (D3)** against the currently sealed envelope. Today it prints one sorted JSON line.
- **The CLI arms the effective revision only (D3, D5)** and refuses when a different revision is staged, naming both.
- **The twin's future shape is fixed now (D4):** a shared Clerk volume, a worker key, and one worker per account enforced in the profiles database. The execution lease is a row in each account's own SQLite under `clerk_dir` (`app/broker/alpaca/clerk/sqlite/repository_lifecycle.py:475`), so it cannot stop two writers on separate volumes.
- **CLI docs target the Clerk volume.** The arming and shadow docs run their CLIs on the host, but the Clerk authority sits on a VM-local volume the host cannot see. Prerequisite 0b.

## 1. Recommended decision

Agree for **user choices**: account nicknames, paper/live configuration, trading limits, ceremony settings, and saved selections belong in a dedicated SQLite profiles database on the Clerk volume (D2). Keep secrets in environment injection. Also keep deployment bootstrap outside user configuration: ports, trusted origins, mounted storage roots (including the Clerk volume that holds the profiles database), and service discovery must exist before a user configuration can be loaded. Fixed protocol rules and mathematical constants remain code-owned.

The desired flow is: create a profile such as **Paper — strategy testing**, select an available credential reference, verify the broker account, save the profile, stage it, and press Apply (D5). Subsequent starts, including crash restarts, load the last-applied selection. The operator can see the account nickname, Paper/Live endpoint mode, actual authority world, and whether the saved configuration is currently running or awaiting application.

Saving a profile, staging it, applying it, approving an account, activating custody, and arming an instance remain distinct operations. A profile never grants order-submission authority.

## 2. Evidence and decisions this changes

Paths below are relative to the implementation checkout; recheck them after the rehearsal changes merge.

| Evidence | Current behavior and consequence |
| --- | --- |
| `PythonDataService/app/broker/alpaca/config.py` | `AlpacaSettings(BaseSettings)` combines credentials, mode, Clerk root, and six live settings. `get_alpaca_settings()` caches one process-wide instance. This is the main source to replace. |
| `PythonDataService/app/main.py` | Startup constructs market liveness, Clerk authority, envelope, and trade-update consumers from that configuration. Moving only the form or settings class leaves runtime consumers inconsistent. |
| `PythonDataService/app/broker/contract/registry.py`, `app/broker/alpaca/broker.py`, `app/broker/alpaca/client.py` | Registry keys are broker IDs, not user/profile/account IDs. Broker reads and lazy client construction reach global settings. Keep one selected context per worker initially. |
| `PythonDataService/app/broker/alpaca/clerk/live_envelope.py`, `live_arming_ceremony.py`, `live_arming_ledger.py`, `sqlite/envelope_admission.py` | Arming names an exact account, instance seal, envelope hash, and session count. Configuration migration must preserve existing numeric values, hashes, and enforcement. |
| `PythonDataService/app/security/data_plane_control.py`, `app/config.py`, `app/routers/broker_v2_panel.py` | Current control authentication is a shared secret; journal actor names come from `PANEL_OPERATOR_IDENTITY`. No authenticated individual-user model was found in the inspected broker path. A request-supplied `user_id` is not ownership enforcement. |
| `Frontend/src/app/components/brokers/alpaca-desk/alpaca-account-card.component.html` | The account card currently displays the broker account ID. A saved nickname needs an explicit projection and shared use across the Alpaca desk and panel. |
| `compose.yaml` (Clerk volume mount), `app/broker/alpaca/clerk/sqlite/activation.py`, `live_arming_ledger.py` | The Clerk authority lives on a VM-local named volume because SQLite WAL needs one locking domain, and operator tools reach it with `podman compose run`. Installation-wide records already live there (`accounts/alpaca/_authority_activations.jsonl`, the arming ledger). The profiles database joins them (D2). |
| `compose.yaml`, `.env.example`, `PythonDataService/.env.example`, `PythonDataService/scripts/manage_alpaca_shadow.py` | Deployment wiring, qualification tooling, and CLI ceremonies consume environment configuration too. They are cutover consumers, not cleanup afterthoughts. |

[ADR 0059](../architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md) explicitly requires environment-sourced live values, and `CONTEXT.md` repeats that rule. Record a successor decision that replaces **the source of configuration**, and only that: it supersedes ADR 0059's environment-source rule, not ADR 0001 or ADR 0035. Preserve mode agreement, activation fences, account isolation, envelope validation, and per-instance arming. Its 2026-09-09 amendment makes shadow optional; do not reinstate a mandatory shadow prerequisite from older headings or prose.

[ADR 0031](../architecture/adrs/0031-cross-stack-boundary-selection-and-contract-generation.md) supports the existing Angular-to-FastAPI path. Ownership: Python owns broker-profile persistence, validation, runtime resolution, and REST contracts, in a dedicated SQLite database on the Clerk volume (D2). Do not add a .NET relay, and do not put profiles inside any account's custody database. [ADR 0001](../architecture/adrs/0001-control-plane-substrate-json-parquet.md) and [ADR 0035](../architecture/adrs/0035-alpaca-clerk-sqlite-event-sourced-authority.md) §6 stand unchanged: no Postgres enters the live control plane. [ADR 0037](../architecture/adrs/0037-sqlite-sole-alpaca-custody-authority.md) and [ADR 0038](../architecture/adrs/0038-alpaca-sole-bot-control-plane.md) continue to govern custody and control intent.

The active checkout had uncommitted rehearsal edits when inspected. In particular, `ALPACA_MARKET_STATUS_UPSTREAM_URL` was being added for a Paper twin to share a Shadow worker's status feed. This is provisional evidence outside the baseline commit. Per D4 the twin stays a separate installation in v1. Refresh this inventory after it lands; preserve its paper-only restriction, credential-free URLs, and shared-feed topology.

## 3. Configuration classification

| Class | Initial examples | Authority |
| --- | --- | --- |
| Secrets | Alpaca key/secret pairs, control secret, Polygon/FRED keys, launcher token, database password | Environment injection; only opaque credential references in profile records |
| User-owned broker configuration | Profile name, account nickname, `ALPACA_MODE`, six `ALPACA_LIVE_*` values, selected credential reference, staged and effective profile/revision | SQLite profiles database on the Clerk volume, validated by Python |
| User/operator identity | Stable local owner ID and operator display label currently supplied by `PANEL_OPERATOR_IDENTITY` | Persisted owner record; server resolves actor/owner, never trusts a browser field |
| Observed/approved identity | Broker account ID and verified mode, approved-account pin | Broker supplies observed identity; explicit verification pins it. Existing activation remains its own authority |
| Deployment bootstrap | `HOST`, `PORT`, origins/trusted hosts, `ALPACA_CLERK_DIR` (which also locates the profiles database), artifact roots, service addresses | Deployment configuration; not editable as a trading profile |
| Shared market-status configuration | Choice to use the supported shared feed; upstream service address | Persist semantic source choice/reference in the profile if user-selectable; keep address resolution in deployment configuration. Do not create arbitrary credential-bearing URL inputs |
| Capability/release gates | `ALPACA_SQLITE_MANUAL_TRADING_ENABLED`, `ALPACA_PAPER_CARRYOVER_ENABLED`, fault injection, unauthenticated-control override, wiring-enforcement rollout flags | Classify individually as release/security/test policy. Do not expose them as profile permission switches or enable deferred capabilities |
| Code invariants | Mode-to-endpoint mapping, reserved account namespaces, numerical tolerances, fixed protocol rules | Code and accepted decisions; not a generic settings table |

The six baseline fields are `live_loss_fraction`, `live_loss_usd`, `live_shadow_sessions`, `live_arming_max_sessions`, `live_xh_entry_bps`, and `live_xh_exit_bps`. Preserve their existing ranges, finite-number checks, requiredness, and canonical serialization. Those checks live only in `AlpacaSettings` today (`app/broker/alpaca/config.py:84-115`), and `LiveEnvelopeValues` validates nothing itself (`clerk/live_arming.py:344`). Move them into one validated type owned by the configuration module, store floats as REAL and session counts as INTEGER, and prove that store → load → sha returns today's sha on every read path (`5000` and `5000.0` hash differently; a `Decimal` raises). Keep `shadow_sessions` as configured rehearsal policy without treating a shadow receipt as an arming prerequisite. Inventory Paper extended-hours consumers as well as Live consumers before removing their environment reads.

Inventory all other app settings during package A. Provider entitlements/throttle preferences and user data-processing choices may merit subsequent profiles; service admission limits and infrastructure identity need different treatment. This plan does not promise that every non-secret becomes a user-editable field.

## 4. Proposed data model and module interface

Use typed domain fields, not an arbitrary key/value environment editor. Proposed record names are design names, not an instruction to create one file per record.

| Record | Minimum content and rules |
| --- | --- |
| Local owner | Stable generated ID, display label, creation/update timestamps. Initial installation is explicitly one local operator; label changes do not change ownership. Preserve historical journal actor strings. |
| Broker profile | Stable ID, owner ID, broker=`alpaca`, display name, archive status. Label-only edits do not alter execution identity. |
| Account nickname | Keyed to the observed broker account ID, not to a profile, so two profiles on one account show one name on receipts and surfaces. |
| Profile revision | Immutable version, profile ID, schema version, credential reference, endpoint mode=`paper|live`, verified account pin when bound, typed policy values, canonical non-secret content hash, author and creation time. Incomplete drafts can be saved but cannot be applied. |
| Installation selection | Staged profile ID and exact revision, a one-shot Apply request, a monotonically increasing selection generation, and a durable last-effective profile/revision/account acknowledgement. One row per installation in v1 (D4); reference ownership and archive status checked transactionally. Only an Apply moves the staged revision to effective (D5). Only the worker writes the acknowledgement, and that historical acknowledgement alone does not prove it is currently running. |
| Configuration event | Actor, action, affected profile/revision, previous and next references, result and timestamp. Append-only, without secret values or secret-derived hashes. |

All timestamps are `int64 ms UTC`. Enforce foreign keys/uniqueness, version-checked writes, and idempotency for retried create/stage/apply operations. Return a conflict for stale edits rather than overwriting a newer revision. Archive profiles while preserving referenced revisions and evidence; refuse removal of staged, effective, or in-use configuration.

Multiple saved profiles are supported. There is one effective profile per installation, matching the runtime topology (D4). A Paper twin run before v2 is its own installation with its own Clerk volume and profiles database. Each credential slot is injected into exactly one worker; across volumes that is the only guard against two writers on one account. Do not introduce concurrent multi-account custody within one process. Preserve the existing one-live-account-per-installation scope of ADR 0059; this change does not expand it.

The **configuration module** presents a small interface: manage profiles/revisions; verify an account; stage a selection and record an Apply; resolve the effective selection into an immutable runtime context; report staged versus effective status. Its implementation owns storage, validation, credential resolution, account pin checks, revision conflicts, and audit. Runtime callers, including the CLIs, receive the resolved context; they do not each query the profiles database or read environment variables.

### Credential references

Use an opaque slot such as `alpaca_paper_primary`; a fixed provider-specific naming convention or deployment allowlist maps it to the two permitted secret variables. Resolve secrets only inside the backend. Do not allow a profile to name arbitrary environment variables, enumerate the whole environment, or supply a free-form API base URL.

Expose only slot labels and availability/verification status through protected endpoints. Never return key fragments, values, raw settings dumps, or credential-bearing validation inputs. Legacy `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY` may map to one explicitly named compatibility slot. Multiple profiles for different actual accounts require corresponding injected secret pairs; a saved nickname does not manufacture access to another account.

Verification performs read-only broker account discovery using the revision's mode and resolved credentials. Store the observed account pin after explicit selection of that observed account; do not ask operators to type account IDs. Re-observe at apply/startup. Missing credentials or an account/mode mismatch refuse activation without replacing the previous pin. Credential rotation against the same account uses controlled reconnection and re-verification; changing accounts requires a new revision and account approval.

### Ownership

Package A confirms whether authenticated users have appeared on the integration branch. If not, implement a durable local-owner record and explicitly document the single-operator deployment model. The existing shared secret authenticates that local control context; it does not identify different people. Protect profile reads, writes, credential availability, staging and Apply with the existing protected-route posture. Reject/ignore client-supplied owner or actor fields according to a closed request schema.

Do not bolt on a login system as a hidden dependency. If separate human accounts are required, authenticated principals and per-owner access enforcement become an explicit prerequisite before claiming multi-user support. Test that a forged owner/profile reference cannot cross the server-resolved owner scope even in the initial model.

## 5. Selection and runtime behavior

1. **Save:** create a draft or immutable revision in the profiles database. It does not mutate an existing runtime, account approval, bot seal, activation record, or arming record.
2. **Stage:** persist the exact revision as the installation's staged selection. UI navigation or choosing a default profile for a form is not staging. Show the current effective revision beside the staged revision and any pending change.
3. **Apply:** the operator presses Apply, which records a one-shot apply request; the staged revision becomes effective at the next controlled restart. A restart with no apply request (a crash, a reboot, `restart.sh`, compose's `restart: always`) boots the last-effective revision (D5). No `ACTIVE_PROFILE` environment variable. Do not add automatic container restarts to the web UI in this scope.
4. **Switch preflight:** a different account/profile or execution-affecting revision cannot replace a runtime with running bots, attributed exposure, unresolved orders, or unproven custody. Refuse and explain the existing stop/flatten/reconciliation actions. Merely pausing is insufficient because PAUSED can suspend EXIT work. Recheck under the lifecycle/lease mechanism at handover, not only in the browser. A refused preflight boots the last-effective revision and shows the staged one as blocked with the backend's reason; it never leaves the worker with no broker.
5. **Build one context:** load and validate the effective revision, resolve secrets, observe account identity, retain existing mode-agreement and activation checks, and construct broker clients/streams/Clerk from the same immutable context. Do not mutate the old settings singleton in place.
6. **Publish effective status:** acknowledge the exact selection generation only after construction succeeds and the worker owns the required execution lease. A stale start/apply attempt must not publish itself as the effective runtime. The UI reports activation failure or pending restart truthfully.
7. **Preserve custody:** a profile ID or nickname never becomes a Clerk account directory, broker account ID, bot seal, or authority generation. Use existing account-rooted isolation. Two profiles referring to the same account cannot create competing writers or different concurrent account risk envelopes. The execution lease is a row in each account's own SQLite under `clerk_dir`, so it only excludes processes sharing that Clerk volume; across installations the guard is D4's one-slot-one-worker rule.

Ordinary recovery/restart of the **same pinned effective configuration** must retain the current ability to recover with positions and resolve EXITs; do not apply the profile-switch flatness requirement to that recovery path. (Boot recovery runs on live; the operator's historical execution recovery is still paper-only, `sqlite/historical_execution_recovery.py:131-141`, and this plan does not change that.) All ways to start with a different revision, including an Apply recorded while the worker is offline, must inspect the durable last-effective binding and detect unresolved prior account obligations before installing a new writer. If prior-account credentials or evidence are unavailable, switching cannot prove that account is clear and must refuse, booting the last-effective revision.

Paper/live endpoint mode and custody world are distinct: a live endpoint may support Shadow before graduation, and Dry Run remains synthetic. Derive/report actual authority from the existing selector and activation facts. A profile mode dropdown cannot mint graduation, synthetic activation, account approval, or per-instance arming. Existing bots remain sealed to their accounts; cloning/redeploying follows existing ceremonies.

### Arming, changes, and failure behavior

- Preserve the existing `LiveEnvelopeValues.sha` encoding and values across the storage migration. Profile ID, owner, label, and credential reference are not risk-envelope inputs. Metadata-only renaming must not invalidate arming.
- Saving a changed risk revision leaves it staged; only the effective revision governs running bots. Once applied, changed envelope/session settings trigger existing arming disagreement or require re-arming. The arming CLI arms the effective revision only, refuses when a different revision is staged (naming both), and prints a before→after diff against the currently sealed envelope. Exits price from the sealed envelope as section 0 describes. Reverting to older content must not silently revive an arming invalidated by an intervening effective change; carry explicit invalidation evidence if current ledger semantics need it.
- Never rewrite historical sealed records. Add no fields to custody, activation or arming records in this project: a rollback means old code reading new records, and a new field in a sealed or hash-chained record breaks it. Profile provenance lives only in the profiles database's own event log. Do not silently recompute old hashes or migrate bot identity.
- Startup with no selection, an invalid revision, missing credentials, or an unreadable profiles database exposes a broker-unconfigured/unavailable state and grants no new broker authority. It must boot, not crash-loop, with existing bindings in `live_state` (the #2014 failure mode), and it must reproduce every degraded state the lazy settings produce today: five call sites catch `ValidationError` when called, and `app/main.py:113-132` decides Clerk installation the same way. Other independent application features retain their existing startup behavior.
- The profiles database shares the Clerk volume's durability, backup and locking domain, so it cannot be lost while custody survives. A running worker resolves its context once; EXIT and reconciliation never read the profiles database per tick. Existing broker, arming, loss, stream-health, and lease gates still govern execution.
- No fallback to stale user settings from environment after cutover. Selection/apply failures never automatically choose the first profile, switch accounts, enable live mode, or re-arm a bot. Staying on the last-effective revision is not a fallback.
- **Proposed, not yet confirmed by the owner:** a retired user setting still present in the environment after cutover (for example a leftover `ALPACA_LIVE_LOSS_USD`) is not silently ignored, which `extra="ignore"` would otherwise do. Boot proceeds, the live verdict names it, and new entries are refused until it is removed; exits keep running.

## 6. Delegatable work packages

Each package is a separate reviewable change with an owner and completion evidence. Agents create their own `codex/` worktrees from the agreed integration commit. Do not start them on the active rehearsal checkout. This plan does not launch agents or create tracker issues.

Merge each package to master before a dependent package branches from it; squash merges break stacked branches. The running containers mount the main checkout, so verify each package in its own worktree on the host venv (`DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m pytest ...`), never with `podman exec`. Tests use tmp-path Clerk roots.

### 0 — Prerequisites (tracked separately)

**0a — Loss hold judged against the sealed envelope** (section 0, D3), with a regression test that fails first. **0b — Arming and shadow CLI docs use `podman compose run`** against the Clerk volume instead of a host run. Both land on master before B–D start.

### A — Freeze the decision, contract, and setting inventory

**Depends on:** rehearsal/integration baseline identified; read-only work can start immediately.
**Assignment:** Reconcile this proposal against the integrated rehearsal code. Record the successor ADR for environment-to-profile authority (superseding only ADR 0059's environment-source rule), SQLite-on-Clerk-volume storage (D2), the initial local-owner model, the staging and re-arm rule (D3), single-worker scope with the twin's future shape (D4), Apply-based application (D5), and exact switch/recovery semantics. Add the required `Vocabulary:` line and minimal live-domain glossary additions. Update only relevant ADR 0059 references and the doc-authority index. Record an inventory assigning every relevant setting to secret, user choice, deployment, release/test policy, or invariant, with callers and migration disposition.

Publish shared request/response/error contracts for profile management, credential availability, account verification, staging and Apply, and staged/effective status. Choose route paths under the existing Alpaca API and UI area after checking collisions. Define revision/idempotency semantics and an ownership/file split for B–E. This is the contract other agents implement, not separate private designs.

**Done when:** exact setting ownership and DTOs are reviewable; account mode vs authority world is unambiguous; accepted decisions being superseded are named; no user setting remains ambiguously read from both the profiles database and environment. Reconcile unresolved product choices with the owner before marking the successor ADR Accepted; do not mistake this plan's proposal for completed acceptance.

### B — Persist and manage named profiles

**Depends on:** A.
**Primary areas:** new focused configuration module under `PythonDataService/app/`; a dedicated SQLite database on the Clerk volume following the Clerk's existing SQLite conventions (WAL, advisory locking, versioned schema); typed schemas/routers and focused tests.
**Assignment:** Implement the local-owner record, typed profiles and immutable revisions, account nicknames, archive/list/read/edit/clone, the installation selection with its Apply request, and audit. Use a dedicated versioned schema with atomic migration/locking, without making it part of any account's custody database. Implement protected owner-scoped routes and generated OpenAPI artifacts.

**Done when:** create/edit/reload/stage/apply survives a service restart and `podman compose down -v`; migrations are repeatable and preserve rows; concurrent stale edits/staging return conflicts; archive rules preserve references; request fields cannot set owner/actor; secret values are absent from rows, API payloads, and logs.

### C — Resolve credentials and verify account identity

**Depends on:** A; may run alongside B using the shared contracts.
**Primary areas:** `app/broker/alpaca/config.py`, `client.py`, `broker.py`, credential resolver, profile-verification implementation and tests.
**Assignment:** Separate environment-only credential loading and deployment bootstrap from validated profile values. Implement allowlisted slots, safe availability responses, read-only account verification and pinning, and an immutable resolved runtime context. Inject mode/settings consistently into broker and client construction. Endpoint URLs stay derived from mode.

**Done when:** two saved profiles can resolve distinct fake credential pairs without cross-contamination; missing slot, arbitrary env lookup, wrong account, stale verification, and paper/live mismatch fail explicitly; verification makes no submit/cancel calls; errors and repr/log paths cannot expose secret material. Rotation does not silently change the account pin.

### D — Apply saved selections throughout the worker

**Depends on:** B + C.
**Primary areas:** `app/main.py`, broker registry/composition, `market_liveness.py`, `trade_updates.py`, `marketable_limit.py`, active-authority selection, `live_arming_ceremony.py`, live envelope adapters, broker snapshots/projections, runner integration, CLI ceremonies.
**Assignment:** Resolve the effective selection at startup (the staged one only when an Apply is recorded) and pass the same context to every consumer. Implement staged/effective revision reporting, generation fencing, switch preflight with last-effective boot on refusal, and recovery distinction. Price exits from the sealed envelope and make the arming CLI arm the effective revision only, with its before→after diff (section 0). Remove non-secret environment reads from migrated paths. Adapt envelope construction without changing the calculations. Include CLI arming/shadow/graduation tools and the rehearsal's shared market-status path. The CLIs resolve settings in their own process today (`scripts/manage_alpaca_arming.py:237`); cover all three: `manage_alpaca_arming.py`, `manage_alpaca_shadow.py`, and `manage_alpaca_sqlite_clerk.py` (its cutover checks `ALPACA_MODE`, `:269`). Land a test seam for the resolved context first: 21 test files touch these settings today and `live_arming_fixtures.py` feeds 10 modules.

**Done when:** restart loads the effective profile; staging never retargets a running bot or creates trading permission; stream/account/cache identities cannot leak from a prior configuration; same-profile recovery with exposure works; different-profile switching with exposure refuses and boots the last-effective revision; a crash with a staged selection boots last-effective; changed effective risk values require re-arming; historical seals still verify; Paper/Shadow/Live/synthetic isolation and one writer per account remain proven.

### E — Build the saved-configuration experience

**Depends on:** A contract; integrate against B–D before completion. May build alongside C/D after generated contract types are available.
**Primary areas:** `Frontend/src/app/components/brokers/alpaca-desk/`, canonical `components/broker/v2-panel/` identity/header/deploy consumers, existing API services and generated types.
**Assignment:** Add profile list/create/rename/clone/edit/archive, credential-slot picker with availability, read-only verified account evidence, staging a selection with an Apply button, and staged/effective status. Display account nickname consistently with Paper/Live and actual authority-world indicators. Show a controlled-restart requirement and backend-authored refusal reasons. Keep account-ID entry out of the setup flow and secrets out of forms and browser storage. Follow current account-ID display policy for ordinary identity surfaces and preserve exact IDs in appropriate audit evidence.

**Done when:** the operator can save “Paper — strategy testing,” reload the page, stage it, press Apply, and observe it become effective after a controlled restart without editing non-secret environment settings. Renaming is durable; stale edits and missing credentials have actionable states; selecting Live does not arm; two browser tabs cannot overwrite newer configuration silently. Use generated contracts, `receiptLabel` for code-like evidence, and `app-asset-identity` for any instrument display. No retired IBKR UI work.

### F — Import existing configuration and cut over

**Depends on:** B–E integrated and validated on isolated fixtures.
**Primary areas:** configuration import tooling/tests, `compose.yaml`, both tracked `.env.example` files, operator manual/runbooks, `manage_alpaca_shadow.py` and other discovered consumers.
**Assignment:** Provide explicit preview/apply import of known non-secret legacy settings for the installation. Write imported profile revisions and selection records idempotently; reference existing secret slots without persisting secrets. Preserve exact account identities, policy values, and envelope hashes. Import operator identity without rewriting history. Report incomplete or ambiguous values; never guess a live limit or silently import a different account. Account observation/pinning must use the same verification seam as normal setup.

Remove migrated user-setting entries from normal runtime environment wiring and update setup/operation instructions. Keep secrets, deployment bootstrap, and qualification/test-only inputs explicitly classified. Limit old user-env readers to the importer and necessary historical tests; do not maintain a second runtime source. Do not remove `ALPACA_MODE` from isolated qualification tooling without replacing its paper-only proof.

**Done when:** preview changes no runtime or profiles-database state; repeated import is idempotent; imported effective envelope hash matches legacy input; empty/partial environments are handled explicitly; configuration reads remain profiles-database-authoritative even when conflicting legacy env values exist. A copy-based rehearsal demonstrates cutover and rollback. Production rehearsal cutover is a separately scheduled operator action, never performed by an implementation agent incidentally, and never while a live instance is armed.

Rollback restores the prior reviewed code/deployment configuration and selection while the affected worker is stopped and its account obligations are reconciled. Retain original config material securely outside the repo and retain new profile records. Never roll back custody databases, activation generations, order journals, or arming ledgers to undo this change.

### G — Integration and independent review

**Depends on:** D + E + F.
**Assignment:** Run the acceptance matrix below against isolated tmp-path Clerk and artifact roots, verify generated contracts and docs, review all changed configuration consumers, and supply a concise release/cutover checklist with test evidence. Use faked broker ports for automated testing; any external account rehearsal needs an explicitly designated isolated account. No test or review script may implicitly use the active rehearsal's credentials, mounts, or execution lease.

**Done when:** matrix evidence is attached, no hidden environment fallback remains, no data-plane math moved into .NET/Angular, and the final diff is limited to the configuration migration and its required docs/tests. The active rehearsal remains untouched.

**Dependency schedule:** 0a and 0b first; A → B and C in parallel; E can start after A's contract is generated; B+C → D; integrated D+E → F → G. B owns shared schemas and generated outputs until handoff; D owns startup/worker composition. Coordinate changes to `config.py` between C and D rather than having both edit it simultaneously.

## 7. Integration acceptance matrix

| Scenario | Required result |
| --- | --- |
| Save/rename/clone/reload | Same owner/profile persists; labels remain distinct from observed account identity and execution hashes |
| Multiple saved profiles | The installation resolves exactly its effective revision and permitted secret slot; no process-global cross-account leakage |
| Conflicting legacy environment | The profiles database wins exclusively after cutover; no merge of user env overrides |
| Credential missing, rotated, or wrong | Availability and verification are truthful; no secret disclosure; wrong account never replaces a pin |
| Forged owner, actor, slot, or profile reference | Protected request is refused; arbitrary environment reads and cross-owner access are impossible |
| Save/stage/apply Live profile | No graduation, arming record, bot start, or real order created |
| Rename vs policy change | Rename preserves arming; applied execution-policy change refuses stale arming; staged change is visibly staged |
| Historical evidence | Existing envelope hashes, seals, and ledgers verify unchanged; no custody, activation or arming record gains a field |
| Active position/order during switch | Apply refuses without pausing EXITs, rewriting bots, or abandoning prior custody; the worker boots the last-effective revision and shows the staged one as blocked |
| Crash with a staged selection and an open position | The worker boots the last-effective revision; EXITs keep running; the staged revision shows as not applied |
| Same-profile crash recovery | Existing recovery and EXIT path works with prior positions under the same pinned context |
| Concurrent staging/start and crash mid-apply | One selection generation is authoritative; stale workers cannot publish or acquire competing custody |
| Full reset (`podman compose down -v`) | Profiles, selection and armings survive together on the Clerk volume; a cold start recovers the last-effective revision |
| Cold start with bindings and no selection | Boots with the gate closed and the reason surfaced; no crash loop (#2014) |
| Paper/Shadow/Live/Dry Run | Correct endpoint and authority isolation; shared status feed retains rehearsal constraints; shadow remains optional |
| Duplicate profiles for one account | No competing writers, no parallel conflicting account envelopes, one nickname, account-based audit identity preserved |
| Loss limit raised after a hold | The hold stands until a re-arm seals the new value; breach and clear are judged against the sealed envelope |
| Staged exit-pricing change | Exits keep the sealed `xh_exit_bps` until re-arm |
| Arming while a different revision is staged | The CLI refuses, naming the staged and effective revisions; an allowed plan shows a before→after diff |
| Store → load → sha round trip | Every read path yields today's `LiveEnvelopeValues.sha` for the same values; float and int types preserved |
| Import twice and rollback rehearsal | No duplicate profiles/selections, secret copies, historical rewrites, or custody rollback |
| Generated contracts and UI | OpenAPI/TypeScript regeneration is clean; persistence, staged/effective state, and backend-authored errors verified |

Read relevant `.claude/rules/python.md`, `testing.md`, and `temporal-rigor.md` before implementation. Frontend agents also use `build-angular-component` and Angular rules. Use `learn-ai-validation` and numerical-rigor rules when touching envelope computation or parity tests; preserve canonical numerical authorities and their existing tests. Every bug fix includes a regression test. Run the documentation-contract and ADR-status checks for decision/doc updates. Do not introduce new dependencies without a concrete need and a rejected existing-library alternative.

## 8. Handoff limits

This file is the only planned change in its worktree. It does not migrate a database, inspect actual `.env` secrets, restart a worker, activate an account, or execute a rehearsal. Before implementation, refresh the baseline and inventory against the agents' integrated work. Keep code changes in independent worktrees with isolated Clerk and artifact roots; a worktree alone does not isolate running infrastructure.

Out of scope: restoring deprecated IBKR product surfaces, Postgres for broker configuration or for Clerk custody or stop intent, multi-worker selection (D4; the twin's future shape is fixed in section 0), new trading math, weakening arming/activation gates, concurrent multi-live-account support, a secret-management UI, and a repo-wide migration of every environment setting in this first delivery.
