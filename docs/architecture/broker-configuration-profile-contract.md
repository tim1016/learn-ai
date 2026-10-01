# Broker configuration profile contract

**Status:** supporting design for [ADR 0060](adrs/0060-broker-configuration-is-a-user-owned-profile-on-the-clerk-volume.md). Lineage: live. Where it disagrees with ADR 0060, the ADR wins.
**Code holds the rest:** the record shapes are `app/broker_configuration/records.py`, the route surface and its request/response schemas are `app/schemas/broker_configuration.py`, and the error taxonomy is `app/broker_configuration/errors.py` and `app/broker/alpaca/profile/errors.py`. The sections that restated them are gone; the kept sections keep their numbers.

## 1. Conventions that are not negotiable

- **Secrets never appear** in a row, a payload, a log line, a `repr`, or an error. Not the value, not a fragment, not a length, not a variable name.
- **Owner and actor are server-resolved.** Request schemas are closed (`extra="forbid"`); a body carrying `owner_id`, `actor`, or `user_id` is refused, not ignored — silently dropping it would let a client believe it had set something.
- **Identifiers** are opaque server-generated strings. A profile ID never becomes a Clerk account directory, a broker account ID, a bot seal, or an authority generation.

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

## 9. Desk truth sources and future-clerk invariants (2026-09-12)

These are frontend presentation rules; they add no contract surface.

### 9.2 Effective Paper/Live identity (truth layers)

- When `effective_choice` exists on the desk state, the identity strip's profile, revision, account label, and endpoint mode come **only** from it. Effective identity is never inferred from staged state.
- Only when `effective_choice` is absent may the generation-zero `BrokerAccountSnapshot` show an *observed* account id and mode — and never a revision, which the snapshot does not have.
- If the effective choice's account id and the independently observed account id disagree, the desk shows an explicit warning and gates identity-dependent actions; the records are never silently merged.

### 9.4 Future clerk support (invariant; spine accepted by ADR 0062)

- Clerk IDs are opaque and backend-issued. The frontend must never fabricate, default, or infer a clerk ID, and must never introduce a `?clerk=` query parameter or a second lens-style storage key for one.
- Every clerk-scoped command must carry the explicit backend-issued ID; presentation-only context objects (e.g. a shared `DESK_CONTEXT`) may expose data already present in server responses and nothing else, and are never command authority.
- Clerk is a **context/lane** dimension (which clerk's surface is in view). Trader/Operator remains the **lens** dimension. The two never merge.
- The fleet spine these invariants anticipated is accepted by ADR 0062 and lands as `PythonDataService/app/broker/fleet/`. It changes nothing in this contract's storage or selection model: each clerk volume keeps its own profiles database and its own single-row installation selection, and ADR 0060's Apply semantics operate per clerk. The worker identity ADR 0060 Decision 5 deferred lives in the fleet registry (`worker_key`), not in any profiles table — the "no `worker_id` column and no workers table" rule (ADR 0060 Decision 5) continues to describe the profiles database exactly.
