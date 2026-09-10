# Alpaca credential slots, the resolved runtime context, and account verification

**Status:** supporting for [ADR 0060](../architecture/adrs/0060-broker-configuration-is-a-user-owned-profile-on-the-clerk-volume.md) (Proposed), Package C of the [user-owned broker configurations plan](../design/user-owned-broker-configurations-plan-2026-09-10.md). Implements [the profile contract](../architecture/broker-configuration-profile-contract.md) §3 and the §7 row for C. Lineage: live.

Package C builds the resolution layer only. Nothing here is wired into
`app/main.py` or the running worker's boot path — Package D composes it — and
this change alters nothing about what the currently-running service does at
boot.

## The slot allowlist

ADR 0060 open question 2 asked whether the allowlist is code-owned or
deployment-declared, and how many slots v1 ships. **The owner decided a fixed,
code-owned closed set of exactly two slots** (2026-09-10). It is a literal in
`app/broker/alpaca/profile/credentials.py`, not a list read from an environment
variable.

| Slot | Key id variable | Secret key variable | Role |
|---|---|---|---|
| `default` | `ALPACA_API_KEY_ID` | `ALPACA_API_SECRET_KEY` | The compatibility slot. Today's unrenamed pair — every current deployment already has it and nothing changes for it. |
| `live` | `ALPACA_CREDENTIAL_LIVE_KEY_ID` | `ALPACA_CREDENTIAL_LIVE_SECRET_KEY` | A second injected pair, following the contract §3 template, so a paper profile and a live profile can be provisioned at once and switching which one is effective does not mean editing `.env` and restarting to change credentials. |

Both are documented in `PythonDataService/.env.example` and the repo-root
`.env.example`, blank by default. This remains single-worker v1 (owner decision
D4): one process, one effective profile at a time. Two pairs may sit in the
environment simultaneously; the *profile* picks which one is in play.

A profile's `credential_slot` must be exactly `"default"` or `"live"`. Anything
else is `422 credential_slot_unknown`, refused **before** any environment object
is built or read — so a profile-supplied string can never become a variable
lookup, and cannot enumerate the environment. The resolver takes no base URL and
no variable name; the endpoint stays derived from `endpoint_mode`
(`_BASE_URL_BY_MODE` in `config.py`).

## What crosses the API, and what never does

Only slot **labels** and **availability** cross: `CredentialSlotAvailability` is
a frozen dataclass of `slot: str` and `available: bool`, and its field set is
pinned by a test. Never a value, a fragment, a length, or a variable name.

Availability is deliberately binary. Reporting *which half* of a pair is missing
would be a fact about the injected secrets that the contract does not admit, so
a half-injected slot (or one whose value is blank or whitespace) reports
`available: false` and refuses resolution rather than half-resolving.

Secret hygiene is asserted, not assumed (contract §8):

- `ResolvedCredentials` holds both halves as `SecretStr`, so the dataclass
  `repr` — and anything that reaches one — shows a mask. `key_id()` and
  `secret_key()` are the two deliberate unwrap sites, called only when handing
  the value to the SDK.
- `AlpacaSettings.api_key_id` / `.api_secret_key` gained `Field(repr=False)`.
  **This closed a real pre-existing leak**: Pydantic's generated `__repr__` and
  `__str__` printed both values, so any log line or f-string that reached a
  settings object printed the live API key.
- `AlpacaRuntimeContext` defines its own `__repr__` naming only profile id,
  revision, mode, slot label and account pin.
- Every error's prose, `next_step`, and `as_detail()` payload is authored from
  constants, allowlisted slot labels, endpoint modes and broker account IDs.
  A rejected slot name is deliberately **not** echoed back.

## The resolved runtime context

`resolve_runtime_context()` turns one revision's non-secret values plus a slot
name into a frozen `AlpacaRuntimeContext`. It holds a fully-specified
`AlpacaSettings` — the same type every existing consumer already accepts — so
Package D changes *where the values come from*, not every consumer's signature:
its migration is `get_alpaca_settings()` → `context.settings` at each call site.
`mode`, `is_paper`, `is_live` and `base_url` are read through `.settings`
rather than mirrored onto the context, keeping `AlpacaSettings` the single place
the endpoint is derived from the mode. The context adds only what settings
cannot carry: the resolved credentials, the slot label, the sealed-envelope
values, the account pin, and the profile/revision provenance.

Two decisions worth recording:

**Validation is not re-implemented.** ADR 0060 Decision 6 requires the domain
checks that live only in `AlpacaSettings` today to move into one validated type
that is the only constructor of `LiveEnvelopeValues` from stored data. Rather
than restate those bounds in a second model — two authorities that would drift —
the resolver builds an `AlpacaSettings` from explicit keyword arguments and lets
it validate. `LiveEnvelopeValues.from_settings` therefore remains the single
construction site, and **store → load → `sha` equals the environment-built
`sha` by construction**, not by a matching pair of validators. A test pins that
equality on the resolve path.

**Type fidelity is checked before Pydantic sees the value.** Pydantic's lax mode
coerces `True` → `1` and `"3"` → `3`, which would silently accept a row that
violates contract §2.4. The resolver asks each stored value for its Python type
by name first — the same `type(value) is int` test `clerk/live_arming.py`
applies to a sealed record — and refuses anything else as `revision_incomplete`.
An `int` supplied for a `float` field is accepted and normalised to `float`,
which is exactly the "convert explicitly on load" the contract asks for.

Every live field is passed to `AlpacaSettings` explicitly, `None` included, so a
stale `ALPACA_LIVE_*` left in the environment cannot contribute a value to a
resolved revision (ADR 0060 Decision 7 — no fallback to a stale user setting).
`ALPACA_CLERK_DIR` is the one value still resolved from the environment inside
`AlpacaSettings`, because it is deployment bootstrap: it must exist before a
profile can be loaded, and a profile must not be able to relocate the custody
volume. `resolve_runtime_context` accepts no `clerk_dir` and no `base_url`
parameter, and a test pins its parameter set.

A `live` endpoint mode without a complete envelope is `422 revision_incomplete`
— the profile-world equivalent of today's `_enforce_mode_agreement` startup
refusal, and never a process crash. `_enforce_mode_agreement`, the six live
constraints and `base_url`/`is_paper`/`is_live` derivation all remain in
`config.py`, unchanged.

## Read-only account verification and pinning

`verify_account()` observes which broker account a context's credentials and
mode actually reach. It talks to `AccountDiscoveryPort`, a `Protocol` declaring
exactly one method, `get_account`. **A port that cannot name `submit` or
`cancel` cannot call them**, which makes "verification performs no mutation" a
structural fact rather than a review finding; a test pins the protocol's method
set and a recording double asserts nothing else was reached for.

Alpaca issues one account per credential pair, so a successful discovery yields
exactly one candidate. The result is still a tuple, because the contract's
verify route returns *candidates* the operator selects from.

The pin rules keep two different operations from being confused:

- `pin_observed_account()` is the only function that produces an `AccountPin`,
  and it requires an explicitly selected account ID that the verification
  actually observed. Nobody types an account ID; no discovery result auto-pins.
- Selecting an account **different from an existing pin** is refused as
  `409 account_pin_mismatch`. Pointing a configuration at a different account
  needs a new revision and fresh account approval.
- `reverify_pinned_account()` — used at apply and at startup — confirms the pin
  and returns the `ObservedAccount`. It has no path that returns a pin, so a
  re-verification can never silently move one. Rotating a secret for the same
  account is therefore controlled reconnection plus re-verification, and the pin
  is untouched.
- A missing credential, a mode disagreement, a wrong account, or a stale
  observation all raise **before** any pin value is produced, so a failed
  re-verification never blanks or changes a previously-pinned account.

`ACCOUNT_VERIFICATION_MAX_AGE_MS` is 5 minutes: long enough for an operator to
read the observed accounts and click one, short enough that the broker's answer
is still the one they are looking at. It bounds a UI ceremony, not risk — no
envelope value derives from it, so ADR 0059's no-defaults-in-code rule for
risk-envelope values does not reach it.

## The interface Package D calls

```python
from app.broker.alpaca.profile import (
    describe_credential_slots,      # () -> tuple[CredentialSlotAvailability, ...]
    resolve_credentials,            # (slot) -> ResolvedCredentials
    resolve_runtime_context,        # (endpoint_mode, credential_slot, live_envelope=None, ...) -> AlpacaRuntimeContext
    verify_account,                 # async (context, discovery=None) -> AccountVerification
    pin_observed_account,           # (verification, selected_account_id, existing_pin=None) -> AccountPin
    reverify_pinned_account,        # (verification, pinned_account_id) -> ObservedAccount
)
```

Every refusal is a `BrokerProfileError` subclass carrying the contract §6
`reason` and `http_status`, with `as_detail()` returning the exact
`{reason, message, next_step}` dict a router hands to `HTTPException`. The
resolver raises rather than returning a union, matching this repo's convention
that domain errors are typed exceptions a router translates; the *availability*
question has its own non-raising answer (`describe_credential_slots`,
`credential_slot_available`) so a router never catches to answer it.

`resolve_credentials` deliberately takes no `mode`. A slot maps to a pair
regardless of endpoint mode; which mode a resolved pair may be used under is the
runtime context's question and the observed account's answer. Keeping the two
apart is what lets one deployment hold a paper pair and a live pair at once.

## Also in this change

`AlpacaBroker` now accepts an injected `AlpacaSettings` and passes it to the
client it builds. It previously read the process-wide singleton for its
capability descriptor and for the account mode it hands the adapter, while
`AlpacaTradingClient` already accepted one — so a broker built for one profile's
context would still have read another configuration's mode. The lazy fallback is
unchanged, so the port is still registered at startup on a credential-free
service.

`live_envelope._SETTINGS_FIELDS` became `ENVELOPE_SETTINGS_FIELDS`. The resolver
builds settings from stored envelope values through the same pairing
`LiveEnvelopeValues.from_settings` reads them back with, so the two directions
cannot drift into separate rename layers.

## Tests

`PythonDataService/tests/broker/alpaca/profile/` — the allowlist and resolver,
the runtime context and its type-fidelity and sha-parity obligations, read-only
verification and the pin rules, settings injection, and a cross-cutting
secret-containment suite that searches every `repr`, `str`, log record and error
payload for the fixture secrets. Fixture credentials are deliberately not
key-shaped: no `PK`/`AK` prefix and no 20/40-character token, so nothing in the
suite could be mistaken for a real credential in a log grep.
