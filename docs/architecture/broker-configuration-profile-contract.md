# Broker configuration profile contract

**Status:** supporting design for [ADR 0060](adrs/0060-broker-configuration-is-a-user-owned-profile-on-the-clerk-volume.md), Package A of the [user-owned broker configurations plan](../design/user-owned-broker-configurations-plan-2026-09-10.md). Lineage: live.
**Audience:** packages B (persistence), C (credentials and verification), D (worker composition) and E (UI). **This is the shared contract they implement — not a starting point for four private designs.** Where it disagrees with a package's own convenience, this file wins; where it disagrees with ADR 0060, the ADR wins.
**Baseline:** route and auth facts verified on disk at `037ffe12`, 2026-09-10.

Package B supplies the shared request/response schemas and generated OpenAPI artifacts. This contract records the accepted design implemented by the integrated packages.

## 1. Conventions that are not negotiable

- **All timestamps are `int64 ms UTC`**, on the wire, at rest, and in every DTO. Field names end `_at_ms`. No ISO strings, no `datetime` on any boundary (`.claude/rules/temporal-rigor.md`). Schema bounds use `MAX_TIMESTAMP_MS`, never `2**63 - 1`.
- **Response fields are `snake_case`** (the .NET and generated-TypeScript consumers expect it).
- **Secrets never appear** in a row, a payload, a log line, a `repr`, or an error. Not the value, not a fragment, not a length, not a variable name.
- **Owner and actor are server-resolved.** Request schemas are closed (`extra="forbid"`); a body carrying `owner_id`, `actor`, or `user_id` is refused, not ignored — silently dropping it would let a client believe it had set something.
- **Identifiers** are opaque server-generated strings. A profile ID never becomes a Clerk account directory, a broker account ID, a bot seal, or an authority generation.

## 2. Record shapes

Typed domain fields, not a key/value editor. These are design names; they do not mandate one file per record.

### 2.1 Local owner — one row

| Field | Type | Notes |
|---|---|---|
| `owner_id` | `str` | Stable, generated once at first boot. Never client-supplied. |
| `display_label` | `str` | Editable. Seeded from `PANEL_OPERATOR_IDENTITY` at first boot; the record is authoritative afterwards. A change here does not change ownership and does not rewrite historical journal actor strings. |
| `created_at_ms`, `updated_at_ms` | `int` | |

Exactly one owner exists in v1. The deployment is documented as single-operator; the shared secret authenticates a control context, not a person (ADR 0060 Decision 3).

### 2.2 Broker profile

| Field | Type | Notes |
|---|---|---|
| `profile_id` | `str` | |
| `owner_id` | `str` | Server-resolved. |
| `broker` | `Literal["alpaca"]` | Closed now; the column exists so a second broker does not need a migration. |
| `display_name` | `str` | Unique per owner among non-archived profiles. Metadata only — never an execution input. |
| `archived` | `bool` | |
| `created_at_ms`, `updated_at_ms` | `int` | |

Label-only edits alter no execution identity and **never invalidate an arming**. Save updates the UI immediately without Apply; the running worker refreshes its display labels at its next restart. Historical records keep their original labels.

### 2.3 Profile revision — immutable

| Field | Type | Notes |
|---|---|---|
| `profile_id` | `str` | |
| `revision` | `int` | Monotonic per profile, starting at 1; gaps after Paper reset are allowed and deleted references are never reused. |
| `schema_version` | `int` | The revision payload's own version, for forward migration. |
| `credential_slot` | `str` | An opaque slot name from the allowlist (§3). Never a variable name or a URL. |
| `endpoint_mode` | `Literal["paper","live"]` | Replaces the runtime read of `ALPACA_MODE`. The API base URL stays **derived** from this and is never a field. |
| `account_pin` | `str \| None` | The broker account ID observed and explicitly selected during verification. `None` on an unbound draft. |
| `account_pinned_at_ms` | `int \| None` | |
| `live_envelope` | object \| `None` | The six values (§2.4). Required when `endpoint_mode == "live"`; `None` is permitted on a paper revision. |
| `content_sha256` | `str` | Canonical hash over the **non-secret** revision content, for idempotency and stale-edit detection. It is **not** the envelope sha and is never mistaken for it. |
| `complete` | `bool` | A draft may be saved; only a complete revision may be staged or applied. |
| `author_owner_id` | `str` | |
| `created_at_ms` | `int` | |

The offline Paper developer reset in ADR 0060 Decision 8 is the sole revision-retention exception. It removes the target account's pinned Paper revisions and associated unpinned Paper drafts while retaining Live and other-account revisions. New revision numbers exceed the maximum in both remaining revisions and immutable configuration events; clients use the latest retained revision for `expected_revision` and the returned revision number.

### 2.4 The live envelope block — the type-fidelity contract

Field names are **exactly** `LiveEnvelopeValues`' field names, so the mapping to the dataclass is an identity and no rename layer can drift:

| Field | Wire/DTO type | SQLite column | Domain |
|---|---|---|---|
| `loss_fraction` | `float` | `REAL` | `> 0`, `< 1`, finite |
| `loss_usd` | `float` | `REAL` | `> 0`, finite |
| `shadow_sessions` | `int` | `INTEGER` | `>= 1`, exactly `int` |
| `arming_max_sessions` | `int` | `INTEGER` | `>= 1`, exactly `int` |
| `xh_entry_bps` | `float` | `REAL` | `>= 0`, `< 10000`, finite |
| `xh_exit_bps` | `float` | `REAL` | `>= 0`, `< 10000`, finite |

**Binding requirements (ADR 0060 Decision 6):**

1. `NUMERIC` columns and `decimal.Decimal` are **banned** on this path. `canonical_sha256`'s `json.dumps` carries no `default=`, so a `Decimal` raises `TypeError` instead of hashing.
2. `5000` and `5000.0` are different envelope documents. The four float fields round-trip as `float`; convert explicitly on load rather than relying on SQLite type affinity.
3. The two counts round-trip as exactly `int` — not `1.0`, not `True`.
4. The validation above lives in **one** validated type owned by the configuration module, and that type is the only constructor of `LiveEnvelopeValues` from stored data. It must not be re-implemented per caller, and it must not be dropped on the way out of `AlpacaSettings`.
5. **Test obligation:** for every read path, `LiveEnvelopeValues` rebuilt from stored data yields the same `sha` as the same values from environment settings, and every historical arming seal still verifies.

### 2.5 Account nickname

| Field | Type | Notes |
|---|---|---|
| `account_id` | `str` | The **observed broker account ID** — keyed to the account, not to a profile, so two profiles on one account show one name everywhere *within this installation*. Across installations, see ADR 0060 open question 4. |
| `nickname` | `str` | |
| `updated_at_ms` | `int` | |

A nickname is an installation-local label. Two installations may name the same account differently; nicknames are not synchronized. Saving a nickname updates the UI without Apply. It never substitutes for the account ID in audit evidence, which preserves exact IDs.

### 2.6 Installation selection — one row in v1

| Field | Type | Notes |
|---|---|---|
| `staged_profile_id`, `staged_revision` | `str \| None`, `int \| None` | What the operator selected. Governs nothing. |
| `apply_requested` | `bool` | The one-shot request. |
| `apply_requested_at_ms` | `int \| None` | |
| `selection_generation` | `int` | Monotonic. Incremented on each stage and each apply request. Fences a stale worker. |
| `effective_profile_id`, `effective_revision` | `str \| None`, `int \| None` | The durable last-effective binding. **Established only by the worker**, after construction succeeds and it owns the required execution lease; offline Paper reset may clear a reference to a removed revision. |
| `effective_account_id` | `str \| None` | The account the worker actually bound. |
| `effective_acknowledged_at_ms` | `int \| None` | A historical acknowledgement. It does **not** prove the worker is running now. |
| `last_apply_outcome` | `Literal["applied","refused"] \| None` | |
| `last_apply_refusal_reason` | `str \| None` | Backend-authored; the UI renders it and never composes one. |

**No `worker_id` column and no workers table** (ADR 0060 Decision 5, owner decision D4).

### 2.7 Configuration event — append-only

`event_id`, `actor_owner_id`, `action`, `profile_id`, `revision`, `previous_ref`, `next_ref`, `result`, `recorded_at_ms`. No secret values and no secret-derived hashes. This log is the **only** home for profile provenance — no field is added to any custody, activation or arming record.

## 3. Credential slots

A profile references an **opaque slot name**, and a fixed provider-specific convention maps that slot to exactly **two** environment variable names. **ADR 0060 open question 2 is resolved** (owner, 2026-09-10): the allowlist is a fixed, **code-owned closed set**, and v1 ships **two** slots — `default` (the compatibility slot, mapping to the unrenamed `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY`) and `live` (`ALPACA_CREDENTIAL_LIVE_KEY_ID` / `ALPACA_CREDENTIAL_LIVE_SECRET_KEY`, on the template below). The template is therefore no longer provisional.

```
<slot>  ->  ALPACA_CREDENTIAL_<SLOT_UPPER>_KEY_ID
            ALPACA_CREDENTIAL_<SLOT_UPPER>_SECRET_KEY
```

Rules:

- The set of admissible slot names is a **closed allowlist**, not free-form. A slot name outside it is refused (`credential_slot_unknown`) — it is never used to build a variable name.
- A profile may **not** name an environment variable, enumerate the environment, or supply an API base URL. The endpoint stays derived from `endpoint_mode`.
- The legacy pair `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY` maps to exactly one explicitly named compatibility slot, so today's deployment keeps working without renaming its variables.
- Only slot **labels and availability/verification status** cross the API. Never a value, a fragment, a length, or a variable name.
- Two profiles pointing at different real accounts require two injected pairs. A nickname manufactures no access.
- Resolution happens only inside the backend, at context construction — never per tick, never in a router.

**Resolved** (ADR 0060 open question 2, owner 2026-09-10 — see above). Built as Package C: [`docs/references/alpaca-credential-slots.md`](../references/alpaca-credential-slots.md).

**Verification** is read-only broker account discovery under the revision's mode and resolved credentials: no submit, no cancel, no mutation of any kind. The operator selects from the observed accounts; nobody types an account ID. The pin is re-observed at apply and at startup. A missing credential, a mode mismatch, or a wrong account **refuses without replacing the previous pin**. Credential rotation against the same account is controlled reconnection plus re-verification; changing accounts requires a new revision and account approval.

## 4. Route surface

**Prefix: `/api/brokers/alpaca/configuration`.** Checked against the live route table at `037ffe12`: `configuration` is not a claimed depth-2 segment under the `/api/brokers/{broker}/…` wildcard routes (claimed today: `account`, `positions`, `orders`, `order-groups`, `activities`, `fees`, `assets`, `clock`, `portfolio-history`, `portfolio-history-proof`, `clerk`, `live-verdict`, `live-envelope`, `panel-profile`, `accounts`, `bots`, `fault-injection`), and no existing route has a wildcard at path position 3. Five routers already share the `/api/brokers` prefix, so **a contract test pins that no `/{broker}` route shadows this prefix** rather than leaving it to registration order.

**This surface implements accepted ADR 0060.** The credential allowlist is code-owned; metadata `PATCH` and nickname saves update the UI immediately without Apply.

**Auth:** identical to the existing control surface — `Depends(require_data_plane_control_secret_always)` on reads and `Depends(require_data_plane_control_secret)` on mutations, header `X-Data-Plane-Control-Secret`, `DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL` unchanged (`app/security/data_plane_control.py`).

| Method | Path (after the prefix) | Purpose | Success |
|---|---|---|---|
| GET | `/owner` | The local owner record. | 200 |
| PATCH | `/owner` | Edit `display_label` only. | 200 |
| GET | `/credential-slots` | Slot labels + availability/verification status. Never values. | 200 |
| GET | `/profiles` | List, with `?include_archived=`. | 200 |
| POST | `/profiles` | Create a profile and its revision 1. | 201 |
| GET | `/profiles/{profile_id}` | Profile + its latest revision. | 200 |
| PATCH | `/profiles/{profile_id}` | Metadata only: `display_name`, `archived`. | 200 |
| POST | `/profiles/{profile_id}/clone` | New profile, copied content, no pin carried over. | 201 |
| GET | `/profiles/{profile_id}/revisions` | Revision history. | 200 |
| POST | `/profiles/{profile_id}/revisions` | Create the next immutable revision. Body carries `expected_revision`. | 201 |
| GET | `/profiles/{profile_id}/revisions/{revision}` | One revision. | 200 |
| POST | `/profiles/{profile_id}/revisions/{revision}/verify-account` | Read-only broker discovery; returns observed candidates. | 200 |
| POST | `/profiles/{profile_id}/revisions/{revision}/account-pin` | Pin one explicitly selected observed account. | 200 |
| GET | `/account-nicknames` | All nicknames. | 200 |
| PUT | `/account-nicknames/{account_id}` | Set one. | 200 |
| GET | `/selection` | Staged **and** effective, plus `selection_generation`, apply state, and any refusal reason. | 200 |
| PUT | `/selection` | Stage an exact `profile_id` + `revision`. Body carries `expected_selection_generation`. | 200 |
| POST | `/selection/apply` | Record the one-shot apply request against the staged revision. | 202 |
| GET | `/events` | Paged configuration event log. | 200 |

Notes that packages must not reinterpret:

- **`PUT /selection` is staging, not switching.** Navigating the UI or picking a default in a form is not staging.
- **`POST /selection/apply` returns `202` and changes no runtime.** It records intent; the effective revision changes at the next controlled restart. There is no `ACTIVE_PROFILE` variable, and **the web UI does not restart containers** in this scope.
- **`GET /selection` always reports both states** so a surface can never render "staged" as if it were running.
- **Only the worker establishes an effective binding.** Offline Paper reset may clear a removed binding. No route writes effective fields.

## 5. Idempotency, conflicts and archive

- **Create/stage/apply are idempotent on retry.** A repeated create with the same `content_sha256` returns the existing revision rather than minting a duplicate; a repeated apply against an already-recorded generation is a no-op success.
- **Stale edits conflict, never overwrite.** `POST /revisions` carries `expected_revision`; `PUT /selection` carries `expected_selection_generation`. A mismatch is `409 revision_conflict` / `409 selection_generation_conflict` naming both values. Two browser tabs cannot silently clobber each other.
- **Archive preserves references.** Archiving hides a profile from the default list and keeps every revision and event. Archiving a profile that is staged, effective, or otherwise in use is refused (`409 profile_in_use`).
- **Writes are version-checked and transactional**, with foreign keys and uniqueness enforced in the schema rather than in application code.

## 6. Error taxonomy

Follow the router convention already in `brokers.py`: a **dict** `detail` with a code-like snake_case `reason` plus operator-readable prose. Request-shape errors stay plain-string `422`s, as `brokers.py:326` and `:362` do today.

```json
{
  "reason": "revision_conflict",
  "message": "This profile changed while you were editing it.",
  "next_step": "Reload the profile and re-apply your change."
}
```

`reason` and `message` are always present. **`next_step` is optional** — the
key is omitted, not null, when the raiser had no step to name (as
`live_envelope_invalid` does for a domain-bound violation, whose `message`
already says which field is wrong). A consumer must treat it as absent rather
than assume the three-key shape.

Two refusal families reach this surface, and both carry the shape above: the
profiles database's own (`BrokerConfigurationError`) and the credential and
verification family the account ceremonies raise (`BrokerProfileError`, package
C's). Both are registered as exception handlers in `app/main.py`; neither may
be left to the catch-all, which would answer a state the contract has words for
with a generic 500.

| `reason` | Status | Meaning |
|---|---|---|
| `profile_not_found` | 404 | |
| `revision_not_found` | 404 | |
| `revision_conflict` | 409 | `expected_revision` is stale. |
| `selection_generation_conflict` | 409 | `expected_selection_generation` is stale. |
| `revision_incomplete` | 422 | A draft cannot be staged or applied. |
| `profile_archived` | 409 | Cannot stage or apply an archived profile. |
| `profile_in_use` | 409 | Archive refused: staged, effective, or in use. |
| `credential_slot_unknown` | 422 | Not on the allowlist. Never used to build a variable name. |
| `credential_slot_unavailable` | 409 | On the allowlist; the injected pair is absent. |
| `account_verification_failed` | 409 | Broker discovery did not resolve an account under this mode. |
| `account_mode_disagreement` | 409 | Observed account contradicts `endpoint_mode`. Reuses the existing live vocabulary. |
| `account_pin_mismatch` | 409 | Re-observation contradicts the pin. The previous pin is **not** replaced. |
| `owner_field_not_accepted` | 422 | The body carried `owner_id` / `actor` / `user_id`. Refused, not ignored. |
| `apply_preflight_refused` | 409 | Running bots, attributed exposure, unresolved orders, or unproven custody. Carries the blocking facts; the worker boots the last-effective revision. |
| `broker_unconfigured` | 503 | No effective selection. The gate is closed and the reason is surfaced; the service still boots (#2014). |
| `profiles_database_unavailable` | 503 | Unreadable database. Fail closed, no new broker authority, no crash loop. |

`reason` values are code-like and stable: the Frontend renders them through the shared `receiptLabel` pipe (CLAUDE.md hard rule), and `message` / `next_step` are backend-authored prose the UI must not compose itself.

## 7. Ownership split for B–E

| Package | Owns | Must not |
|---|---|---|
| **B** | The profiles database, its schema and migrations, the shared Pydantic schemas, the routes above, the generated OpenAPI artifacts. | Touch `config.py`'s credential loading, or any account custody database. |
| **C** | The slot allowlist and resolver, read-only account verification and pinning, the immutable resolved runtime context, mode/settings injection into broker and client construction. | Decide open question 2 alone; let a URL or a variable name become configurable. |
| **D** | Startup resolution of the effective selection, generation fencing, the switch preflight, exit pricing from the sealed envelope, the arming CLI's effective-only rule and diff, and removing migrated environment reads. | Read the profiles database per tick, or let a staged revision reach a running bot. |
| **E** | The profile UI, the slot picker, verified-account evidence, staged-vs-effective display, and the Apply button. | Compose a verdict or a refusal reason; accept an account ID typed by hand; put a secret in a form or in browser storage. |

`config.py` is edited by **C**, not by B and D simultaneously (plan §6 dependency schedule). B owns the shared schemas until handoff; D owns startup and worker composition.

## 8. Test obligations this contract creates

- Store → load → `LiveEnvelopeValues.sha` equals today's sha on **every** read path; float and int types preserved; historical seals still verify (§2.4).
- No `/{broker}` route shadows `/api/brokers/alpaca/configuration` (§4).
- A request body carrying `owner_id` / `actor` / `user_id` is refused with `owner_field_not_accepted`.
- A slot name outside the allowlist never reaches an environment lookup.
- Secret values appear in no row, payload, log, or error — asserted, not assumed.
- Two concurrent stale edits produce a conflict, not a silent overwrite.
- A crash with a staged selection and an open position boots the last-effective revision and keeps EXITs running.
- A refused apply preflight boots the last-effective revision and does not leave the apply request pending.

## 9. Desk truth sources and future-clerk invariants (2026-09-12)

Recorded from the lens/Paper-Live UX simplification (task `.agents/tasks/2026-09-12-alpaca-lens-paper-live-ux.md`). These are frontend presentation rules; they add no contract surface.

### 9.1 Lens dimension (Trader/Operator)

- One shared lens kernel lives at `Frontend/src/app/components/broker/shared/lens/` (type/parser, accessible tablist, preference service, URL helpers). The Alpaca account desk, the full bot panel, and the triage detail all render it; no host keeps a private copy of the tablist or its keyboard handling.
- The names `Trader` and `Operator` and the URL values `trader` | `operator` are user-facing vocabulary and are not renamed.
- Exactly one storage key exists: `learn-ai.alpaca-desk.lens`. The routed desk and the full bot panel share it. The triage detail keeps a purely component-local lens and never reads or writes the preference.
- Precedence is `?lens=` > stored preference > `trader`. Lens switches navigate with `replaceUrl: true` and query-parameter merging so deep links and unrelated parameters survive.
- Lens is a presentation dimension only; it never gates authority. Operator evidence/journal reads stay parked behind an actual Operator act (audit semantics unchanged).

### 9.2 Effective Paper/Live identity (truth layers)

- When `effective_choice` exists on the desk state, the identity strip's profile, revision, account label, and endpoint mode come **only** from it. Effective identity is never inferred from staged state.
- Only when `effective_choice` is absent may the generation-zero `BrokerAccountSnapshot` show an *observed* account id and mode — and never a revision, which the snapshot does not have.
- If the effective choice's account id and the independently observed account id disagree, the desk shows an explicit warning and gates identity-dependent actions; the records are never silently merged.

### 9.3 Stage → Apply → Restart tracker

- The adopted `SelectionResponse` is authoritative for staged/effective and Apply progress.
- Desk lifecycle copy/labels are used only while `desk_state.selection_generation` equals the adopted response's `selection_generation`. On mismatch the tracker renders "Refreshing / state unknown" and Stage/Apply writes are disabled until a newly adopted response agrees.
- The browser may display or copy the restart command but never initiates a restart or arms Live trading (ADR 0060/0059 unchanged).

### 9.4 Future clerk support (invariant, not implemented)

- Clerk IDs are opaque and backend-issued. The frontend must never fabricate, default, or infer a clerk ID, and must never introduce a `?clerk=` query parameter or a second lens-style storage key for one.
- Every clerk-scoped command must carry the explicit backend-issued ID; presentation-only context objects (e.g. a shared `DESK_CONTEXT`) may expose data already present in server responses and nothing else, and are never command authority.
- Clerk is a **context/lane** dimension (which clerk's surface is in view). Trader/Operator remains the **lens** dimension. The two never merge.


## Integration enforcement (Package G)

The installation has one worker ownership lock and a separate selection handover
lock. Selection mutations and arming plan/apply cannot overlap a worker's
preflight-to-acknowledgement handover. The selected broker's own account response
must match the revision pin before any custody opens; a stale acknowledgement
closes the candidate runtime before installation or background tasks.

Changing a profile or execution revision requires prior obligations to be clear,
even for the same broker account. A refused Apply consumes its request and
recovers last-effective, including refusals caused by retired environment lines.

An effective live-envelope change appends `live_arming_invalidated` configuration
events. `previous_ref` is the exact arming record hash, `next_ref` its account ID,
and the profile/revision identifies the effective selection that invalidated it.
The event and effective acknowledgement commit together. Reverting values never
revives these records; a new CLI arming can grant permission again. Existing
arming/custody/activation records and envelope hashes retain their original shape.

### Offline Paper configuration cleanup

Developer reset holds the installation-worker and selection-handover locks and one profiles transaction across the custody reset. It refuses inaccessible configuration and any historical Live pin for the target before moving custody. It removes only the Paper configuration described in §2.3 and the target nickname, preserves owner and append-only events (including Live invalidation fences), clears selection references to deleted revisions, cancels pending Apply, and keeps selection generation positive and monotonic. An empty post-reset installation is broker-unconfigured until a fresh Apply; it cannot fall back to environment bootstrap. A failure after durable custody publication leaves the old authority fenced and configuration unchanged; retry resumes that receipt and completes cleanup.
