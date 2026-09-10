# ADR 0060 — Broker configuration is a user-owned named profile stored on the Clerk volume, and only an explicit Apply makes one effective

**Status:** Proposed
**Provenance:** The delegation plan [`user-owned-broker-configurations-plan-2026-09-10.md`](../../design/user-owned-broker-configurations-plan-2026-09-10.md) and the five owner decisions recorded in its section 0 on 2026-09-10 (D1 scope, D2 storage, D3 loosening, D4 workers, D5 switching). Written as Package A of that plan against `037ffe12` (PR #2017 merged). This ADR is **Proposed, not Accepted**: the plan's own Package A gate says unresolved product choices are reconciled with the owner before acceptance, and the open questions below are exactly those. Every code fact cited here and in the inventory was verified on disk at `037ffe12`. The plan itself is committed **unchanged**, as historical input: it was authored in a separate worktree against baseline `08c55fda` and still carries that worktree's local paths and a few incidental line citations that do not match `037ffe12` (its `config.py:84-115` for the envelope validation is really `config.py:81-90` plus the `92-107` validator). Where the plan and this ADR or the inventory disagree on a code fact, the verified one wins; where they disagree on a *decision*, the plan's section 0 wins.
**Decision drivers:** [ADR 0059](0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md) Decision 4 requires every risk-envelope value to come from the environment file, with no defaults in code. That was the right call when the only operator was the person editing `.env` on the host; it is the wrong call once the operator wants to keep several named configurations and choose between them from a browser. Editing `.env` is also not a *record*: it has no author, no timestamp, no previous value, and no way to say "this one is staged and that one is running." What must not change is the safety chain that ADR 0059 built on top of those numbers — mode agreement, the activation fence, account isolation, per-instance arming, and the sealed envelope sha.
**Related:** [ADR 0001](0001-control-plane-substrate-json-parquet.md) (no Postgres in the live control plane — **unchanged**), [ADR 0031](0031-cross-stack-boundary-selection-and-contract-generation.md) (Python owns the data-plane REST contract), [ADR 0035](0035-alpaca-clerk-sqlite-event-sourced-authority.md) §6 (no PostgreSQL in the Clerk's scope — **unchanged**), [ADR 0037](0037-sqlite-sole-alpaca-custody-authority.md) (SQLite is the sole custody authority), [ADR 0038](0038-alpaca-sole-bot-control-plane.md) (control intent stays file-backed), [ADR 0042](0042-sealed-signal-and-account-scoped-custody-authorities.md) (one semantic seam; exact-identity authority selection), [ADR 0022](0022-temporal-authority-calendar-and-timestamp.md) (`int64 ms UTC`), [ADR 0047](0047-authority-recovery-is-an-offline-ceremony.md) (recovery is offline, never a panel action), [ADR 0059](0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md) (its environment-source rule superseded here in the four places it is stated; see Supersedes).
**Vocabulary:** `CONTEXT.md` § "Broker configuration profiles" — owed, and added in the same change that moves this ADR to Accepted. A Proposed ADR does not put unbuilt language into the live glossary; the terms this ADR will own are listed under Consequences so the section can be written from them.
**Supersedes:** [ADR 0059](0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md)'s environment-source-of-truth rule for the six `ALPACA_LIVE_*` values, **and only that rule**. ADR 0059 states it in four places, so all four are named rather than left standing in contradiction: Decision 4's "**Every envelope value comes from the environment file**"; Decision 3's "`max_sessions` is `ALPACA_LIVE_ARMING_MAX_SESSIONS` from the environment file"; Decision 5.3's "The two anchors are environment settings under Decision 4's rule"; and the Considered-and-rejected line "Envelope defaults in code". In each, **"the environment file" now reads "the effective profile revision"**, and nothing else in those sentences changes. ADR 0059's Provenance line and its "Resolved before Accepted" item 3 are **history and are not rewritten** — they record what the owner decided on 2026-09-07, which was true then. Everything else in ADR 0059 stands unchanged and unweakened: required-when-live, in-domain validation, no-defaults-in-code, sealed-at-arming, `LIVE_ENVELOPE_DISAGREEMENT`, mode agreement, the activation fence, account isolation and per-instance arming. This ADR **does not touch ADR 0001 or ADR 0035** — see Decision 2.

## Context

Six numbers govern real money on the Alpaca path: `ALPACA_LIVE_LOSS_FRACTION`, `ALPACA_LIVE_LOSS_USD`, `ALPACA_LIVE_SHADOW_SESSIONS`, `ALPACA_LIVE_ARMING_MAX_SESSIONS`, `ALPACA_LIVE_XH_ENTRY_BPS` and `ALPACA_LIVE_XH_EXIT_BPS`. Today they are `AlpacaSettings` fields read from the process environment, and `AlpacaSettings` is the only place any of them is validated: `LiveEnvelopeValues` (`PythonDataService/app/broker/alpaca/clerk/live_envelope.py`) is a frozen dataclass with **no field validation of its own**, which `live_arming.py`'s validator says in a comment and then partially compensates for with an `_is_int` guard on the two session counts.

The same environment file also holds things that are not user choices at all — API secrets, the host and port, the Clerk directory, capability-release flags — so "move the settings to a database" is not one decision but a classification followed by a storage decision. The classification is Decision 1; the storage is Decision 2.

Two further facts shape the design and are easy to get wrong:

- **The envelope sha is a hash over Python values, not over numbers.** `LiveEnvelopeValues.sha` is `canonical_sha256(asdict(self))`, and `canonical_sha256` is `sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True))` (`clerk/sealed_ledger.py`). There is no `default=` on that `json.dumps`, so a `Decimal` raises `TypeError` rather than serializing; floats use Python's shortest repr, so `5000` and `5000.0` are different documents; and the two session counts are type-checked to be exactly `int`, so `1.0` and `True` are refused. Every arming record already in an operator's ledger is sealed over that exact encoding.
- **The Clerk volume outlives Postgres.** `my-postgres` is expendable by design — the README's full reset is `podman compose down -v`, which deletes `pgdata`. The Clerk volume is `learn-ai-alpaca-clerk-data`, declared `external: true` in `compose.yaml` and mounted at `/app/artifacts/alpaca_clerk`; `python-service` has no `depends_on: db`. Custody, the activation fence (`accounts/alpaca/_authority_activations.jsonl`) and the live arming ledger already live there.

## Decision

### 1. Configuration is classified, and only one class moves

Every setting **on the Alpaca broker path** belongs to exactly one of six classes. The per-setting assignment, with callers and migration disposition, is [`docs/architecture/alpaca-configuration-ownership-inventory.md`](../alpaca-configuration-ownership-inventory.md); this ADR owns the classes and the rule that each class has exactly one authority. The inventory also classifies the rest of `app/config.py` at class level, without a caller sweep, so the six classes are shown to cover the whole surface — but this ADR decides authority only for the Alpaca path, and a later decision moves anything else.

| Class | Authority | Reachable from a browser? |
|---|---|---|
| **Secret** | Environment injection only. A profile may name an opaque credential *slot*, never a value, never a variable name, never a URL. | Never. Availability and verification status only. |
| **User-owned broker configuration** | The profiles database (Decision 2). | Yes — staged freely, effective only on Apply. |
| **User/operator identity** | The durable local-owner record (Decision 3). | Display label only. |
| **Deployment bootstrap** | Environment injection / `compose.yaml`. It must exist before a profile can be loaded, including the Clerk directory that *locates* the profiles database. | Never. |
| **Capability/release gate** | Code and deployment policy, classified one at a time. | Never. A gate is not a profile permission switch, and a profile may not enable a deferred capability. |
| **Code invariant** | Code and accepted decisions. | Never. |

This ADR does not promise that every non-secret becomes user-editable. In this delivery the migrating set is the six live values, `ALPACA_MODE` as a per-revision endpoint mode, the credential slot reference, and the operator display label. Everything else keeps its current authority until a later decision moves it.

### 2. A dedicated SQLite profiles database on the Clerk volume — not Postgres, and not inside any account's custody database

The profiles database is a **new, separate SQLite database on the Clerk volume**, a sibling of `accounts/`, on the same volume as the activation fence (`accounts/alpaca/_authority_activations.jsonl`) and the live arming ledger, sharing that volume's durability, backup and locking domain. It follows the Clerk's existing SQLite conventions — WAL, the VM-local-filesystem guard, a `control_meta`-style single metadata row carrying a schema version, idempotent versioned migration under a cross-process lock, and `fcntl`/advisory-file locking for the same-host coordination the Clerk already uses (`clerk/sqlite/repository_lifecycle.py`, `app/utils/advisory_lock.py`).

Postgres is rejected for the live control plane, and that rejection is **not new here**: ADR 0001 and ADR 0035 §6 already made it, and **this ADR leaves both exactly as they are**. It is written down again only so a future reader who sees a database appear does not conclude that the Postgres bar was lifted. The independent reasons that make the Clerk volume the right home rather than merely an allowed one:

- A full reset deletes `pgdata` and keeps the external Clerk volume, so Postgres storage would let the profiles vanish while the custody and arming records they explain survive.
- `python-service` has no `depends_on: db`; a Postgres-backed profile would add a *database* startup dependency to the broker path that does not exist today (the service does depend on `redis`).
- Postgres tables share one `public` schema and one superuser with EF Core, which already carries a raw-SQL `DROP TABLE` migration against a snake_case table name.

The profiles database is **not** part of any account's custody database, and **no field is added to any custody, activation or arming record**. Those records are sealed or hash-chained; a rollback means old code reading new rows, and a new field breaks the seal. Profile provenance lives only in the profiles database's own append-only event log.

### 3. One local owner, resolved by the server, never supplied by a request

No authenticated-principal model exists on the inspected path: `PythonDataService/app/security/` contains only `data_plane_control.py` and an `__init__.py`, and journal actor strings come from the `PANEL_OPERATOR_IDENTITY` setting. The shared secret authenticates a *control context*, not a person.

So v1 implements a **durable local-owner record** — stable generated ID, display label, created/updated `int64 ms UTC` — and documents the deployment as explicitly single-operator. The server resolves owner and actor from that record. A client-supplied `owner_id`, `actor` or `user_id` is not ownership: request schemas are closed and reject those fields rather than honouring them. Renaming the label does not change ownership, and historical journal actor strings are preserved as written.

Every profile route is protected exactly as the existing control surface is: the `X-Data-Plane-Control-Secret` header via `require_data_plane_control_secret_always` for reads and `require_data_plane_control_secret` for mutations, with `DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL` unchanged. Multi-user support is **not** claimed and is not a hidden dependency: if separate humans are ever required, authenticated principals and per-owner enforcement become an explicit prerequisite, decided then.

### 4. Staged, effective and sealed are three different things, and only an Apply moves the middle one

A live value therefore exists in up to three states at once, and every DTO, surface and receipt must be able to say which one it is naming:

- **Staged** — saved in a profile revision and selected as the installation's staged selection. It governs nothing. Per D3 the browser may stage any value freely.
- **Effective** — the revision the running worker resolved at boot. It is what the arming CLI may seal and what an unarmed path reads.
- **Sealed** — the envelope values inside a specific `LiveArmingRecord`. It is what governs a running armed instance.

The rules that follow (D3, D5):

1. **Apply is the only transition from staged to effective**, and it is a one-shot request against an exact revision under a monotonically increasing selection generation. Pressing Apply changes no runtime by itself: **the staged revision becomes effective at the next controlled restart**, which the operator performs — the web UI does not restart containers in this scope. What is rejected is not "a restart applies it" but "an *unattended* restart applies it": without a recorded Apply, no restart of any kind changes the effective revision. There is no `ACTIVE_PROFILE` environment variable.
2. **A crash, a reboot, `restart.sh`, or compose's `restart: always` boots the last-effective revision** — never the merely-staged one.
3. **A refused Apply preflight also boots the last-effective revision** and shows the staged one as blocked with the backend's reason. The worker is never left with no broker while positions could be open. A refused Apply **consumes** its one-shot request rather than leaving it pending. This last clause is authored by this ADR, not restated from D5: the owner decided only that a refusal boots last-effective. It is recorded here rather than in the open questions because leaving the request pending would let a later unattended restart apply exactly the change that was just refused, which contradicts D5's own rule — but the owner may still overrule it.
4. **A changed effective value does not reach live entries or exits by itself.** It reaches ENTER only through a re-arm that seals it, and it reaches EXIT pricing only through the same re-arm — exits price from the newest armed record's sealed envelope, falling back to the effective revision only when no armed record is readable, and an exit is never blocked for want of a seal. The loss hold is likewise judged against the sealed envelope, so raising a limit and restarting cannot release a same-day hold. (The loss hold is the plan's **prerequisite 0a**, landing on master before packages B–D start. Exit pricing from the sealed envelope is **package D**, not a prerequisite — B–E must not assume it has already landed. This ADR states the invariant both establish and redesigns neither.)
5. **The arming CLI arms the effective revision only.** It refuses when a different revision is staged, naming both, and its plan prints a before→after diff against the currently sealed envelope instead of one sorted JSON line.

Metadata is deliberately outside this machinery: profile ID, owner, display name, account nickname and credential slot reference are **not** risk-envelope inputs, so a rename never invalidates an arming.

### 5. One worker per installation in v1, and no worker identity

There is exactly one worker, one effective profile and one row of installation selection. **No `worker_id` column and no workers table** is introduced — inventing an identity for a concept with one instance would be an abstraction with no second case to shape it.

The execution lease that excludes a second writer today is a row in `control_meta` inside each account's own SQLite under `clerk_dir` (`clerk/sqlite/repository_lifecycle.py`), so it only excludes processes sharing that Clerk volume. That is why a Paper twin run before v2 is **its own installation** — its own Clerk volume, its own profiles database, its own port — and why each credential slot is injected into exactly one worker: across volumes, that injection is the only guard against two writers on one account. Cross-installation multi-worker selection needs a different mechanism (a shared Clerk volume, a worker key, one-worker-per-account enforced in the profiles database) and is **deferred**, named here so it is designed rather than discovered.

ADR 0059's one-live-account-per-installation scope is preserved unchanged; this ADR does not widen it. Concurrent multi-account custody in one process remains out of scope.

### 6. Store → load → sha must return today's sha, bit for bit

This is the single easiest way to silently break every arming record already in an operator's ledger, so it is a decision, not an implementation note.

The six values are stored and returned as **exactly the Python types `LiveEnvelopeValues` declares**: `loss_fraction`, `loss_usd`, `xh_entry_bps`, `xh_exit_bps` are `float`; `shadow_sessions` and `arming_max_sessions` are `int`. Concretely:

- SQLite columns are `REAL` for the four floats and `INTEGER` for the two counts. **`NUMERIC` is banned**, and `decimal.Decimal` must never reach the envelope — `canonical_sha256`'s `json.dumps` has no `default=`, so a `Decimal` raises `TypeError` rather than hashing.
- A stored `5000` and a stored `5000.0` are **different envelope documents**. A float field must round-trip as a float; SQLite's type affinity must not be relied on to preserve that, and the load path converts explicitly.
- The two counts must round-trip as exactly `int` — not `1.0`, not `True`. `live_arming.py`'s `_is_int` guard exists because the dataclass cannot enforce this itself; the profiles database is now a second place that must not violate it.
- The validation that lives only in `AlpacaSettings` today (`loss_fraction` in (0, 1); `loss_usd` > 0; both session counts `int` ≥ 1; both bps ≥ 0 and < 10000; every value finite, no NaN or inf) **moves into one validated type owned by the configuration module**, and that type is the only constructor of `LiveEnvelopeValues` from stored data. Losing this on the way out of `AlpacaSettings` is how an out-of-domain value reaches an envelope that validates nothing.

The obligation is stated as a test: for every read path, `LiveEnvelopeValues` reconstructed from stored data yields the **same `sha`** as the same values reconstructed from environment settings. Historical seals must still verify after the migration.

### 7. Missing or invalid configuration closes the gate; it never crashes the boot and never falls back to the environment

Startup with no selection, an invalid revision, a missing credential slot or an unreadable profiles database exposes a **broker-unconfigured / unavailable** state, grants no new broker authority, and **boots** — it does not crash-loop. That failure mode is not hypothetical: it is #2014, where live mode without the `ALPACA_LIVE_*` values and a stale `live_state` binding produced exactly that loop. Every degraded state the lazy settings produce today must be reproduced, including the five degraded-state call sites that tolerate an unusable settings object (four catching `ValidationError` by name, one catching broadly and failing closed) and `app/main.py`'s Clerk-installation decision. Other application features keep their existing startup behaviour.

After cutover there is **no fallback to a stale user setting in the environment**. A selection or apply failure never automatically picks the first profile, switches accounts, enables live mode, or re-arms a bot. Staying on the last-effective revision is not a fallback — it is the pinned state.

A running worker resolves its context **once**. EXIT and reconciliation never read the profiles database per tick.

## Considered and rejected

- **Postgres for broker configuration.** Rejected for the three independent reasons in Decision 2 — expendable volume, no `depends_on: db`, shared schema and superuser with EF Core — on top of ADR 0001 and ADR 0035 §6 already barring it.
- **Profiles inside an account's custody database.** The profiles record is installation-wide and outlives any one account; putting it in a custody database couples configuration to a database that recovery ceremonies move aside (ADR 0047).
- **A new field on a custody, activation or arming record to carry profile provenance.** Those records are sealed or hash-chained; a rollback means old code reading new rows.
- **A generic key/value settings table with an arbitrary environment-variable editor.** It would let a profile name any variable and read any secret. Typed domain fields and an opaque credential slot are the whole point.
- **A request-supplied `user_id` as ownership.** It authenticates nothing; anyone who can reach the protected route can claim any owner.
- **"Next restart applies" (no explicit Apply).** Indistinguishable from a crash restart, so an operator could not tell whether a staged change was about to take effect. D5.
- **Letting the browser change a live value that takes effect immediately.** It would bypass the arming ceremony that ADR 0059 exists to impose. D3.
- **A `worker_id` column in v1.** One worker, no second case; the twin is a separate installation. D4.
- **Storing the six values as `NUMERIC`/`Decimal` for "precision".** It changes or breaks the sha over every existing arming record. Decision 6.
- **A paper-only first phase.** Owner decision D1: v1 covers the six live envelope values too.
- **Retiring `ALPACA_MODE` everywhere.** The qualification tooling asserts `ALPACA_MODE=paper` as its paper-only proof; it keeps the variable until an equivalent proof replaces it.

## Consequences

- ADR 0059 carries a supersession note naming this ADR and the one clause it replaces. `docs/references/alpaca-live-envelope.md` and `docs/references/alpaca-live-arming.md` change "environment settings" to "the effective profile revision" and keep every other word.
- The contract for packages B–E is [`docs/architecture/broker-configuration-profile-contract.md`](../broker-configuration-profile-contract.md): record shapes, credential-slot scheme, route surface and error taxonomy. It is the shared contract, not one agent's private design.
- The route surface is a new literal prefix `/api/brokers/alpaca/configuration`. `configuration` is not a claimed depth-2 segment under the existing `/api/brokers/{broker}/…` wildcard routes, so no existing route shadows it; a contract test pins that.
- `CONTEXT.md` gains § "Broker configuration profiles" when this ADR is Accepted, owning: *broker profile*, *profile revision*, *staged selection*, *effective revision*, *Apply*, *credential slot*, *account nickname*, *local owner*, *installation*.
- The `.env` files keep every secret and every deployment-bootstrap variable. The migrated non-secret entries are removed from normal runtime wiring by package F, with the importer and the historical tests as the only remaining readers.
- **Not decided by this ADR:** the manual-order path stays paper-only; no secret-management UI; no repo-wide migration of every environment setting.
- **A pre-existing conflict this ADR surfaces rather than resolves.** ADR 0059 Decision 11 decided that `historical_execution_recovery` **admits live** ("recovery matters more on real money, not less"). The code still refuses it — `clerk/sqlite/historical_execution_recovery.py::_require_paper_account` — and `docs/references/alpaca-live-authority.md` records that as a named residual of slice 7, not as a reversal. So the honest statement is: **paper-only in the code today, live-admitting by decision, re-meaning tracked as a follow-up slice.** This ADR changes neither the code nor ADR 0059 D11, and packages B–G must not read this bullet as authority to close the gap either way.

## Open questions for the owner (answer before this ADR is Accepted)

These are not gaps in the plan — they are choices D1–D5 did not make, and each is recorded rather than guessed.

1. **A retired user setting left in the environment after cutover.** The plan proposes, explicitly unconfirmed, that a leftover `ALPACA_LIVE_LOSS_USD` is *not* silently ignored (which `extra="ignore"` would do): boot proceeds, the live verdict names it, new entries are refused until it is removed, and exits keep running. Accept that, or prefer a quieter treatment (name it on the verdict without refusing entries)?
2. **The credential-slot allowlist.** A slot is opaque (`alpaca_paper_primary`) and maps to a fixed pair of environment variable names — never a free-form lookup. Two shapes are open: a **code-owned closed set** of slot names, or a **deployment-declared list** the operator enumerates. And how many slots does v1 ship — one compatibility slot for the legacy `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY` pair, or that plus a named live slot?
3. **Developer clean-slate reset versus the profiles database.** `dev_reset` moves authority aside rather than deleting it, and ADR 0059 Decision 10 makes it a hard refusal against a live or shadow authority. The profiles database is installation-wide, not account-rooted, so it is outside that rule today. Should a dev reset leave it untouched (the safe default, since wiping it strands the worker with no broker), move it aside with the rest, or refuse whenever a live selection is effective?
4. **Nicknames across installations.** A nickname is keyed to the observed broker account ID inside one installation's profiles database. Under D4 a Paper twin is a separate installation with its own database, so the same broker account could carry two different nicknames on two surfaces. Acceptable, or does the nickname need a shared home?
5. **Whether a metadata-only edit needs an Apply.** A rename or a nickname change alters no execution identity and must not invalidate an arming. But the worker resolves its context once at boot and never reads the profiles database per tick, so a rename reaches the panel immediately and the worker's own resolved context only at the next restart. Is that split acceptable, or should metadata be re-read live so both surfaces agree?
