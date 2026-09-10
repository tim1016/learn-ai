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

Secret hygiene is asserted, not assumed (contract §8). Two pre-existing leaks
were found by the independent security review of this package and closed here:

- **`AlpacaSettings` printed and serialised both credential values.** Pydantic's
  generated `__repr__`/`__str__` printed them, and `model_dump()`,
  `model_dump_json()` and `dict()` returned them in the clear — so any log line,
  f-string, or payload that reached a settings object carried the live API key.
  Both fields are now `SecretStr` with `Field(repr=False)`: `repr`/`str` omit
  them and every serialisation renders `**********`. `.get_secret_value()` is
  the single greppable unwrap idiom, called at exactly four places — the SDK
  client, the two websocket auth frames, and the HITL capture script. `repr=False`
  alone would have closed only one of those five surfaces.
- **A refused revision leaked a credential fragment into the traceback.**
  Pydantic captures the *raw input* in a failed validation's `input_value`, and
  for a model-level validator that input is the whole kwargs dict with
  `loc == ()` — so a per-field filter could not see it, and the fragment reached
  `__cause__`, `traceback.format_exception`, and any `logger.exception`. Two
  changes close it: the resolver hands `AlpacaSettings` the `SecretStr` objects
  still wrapped, so the worst case renders `SecretStr('**********')`, and the
  exception chain is severed outright (`raise … from None`).
  `alpaca_configuration_error_detail` keeps only Pydantic's `msg` text, which
  never echoes the input, so nothing diagnostic is lost.

The rest:

- `ResolvedCredentials` holds both halves as `SecretStr`; they go into
  `AlpacaSettings` still wrapped and are never unwrapped in this package.
- `AlpacaRuntimeContext` defines its own `__repr__` naming only profile id,
  revision, mode, slot label and account pin.
- Every error's prose, `next_step`, and `as_detail()` payload is authored from
  constants, allowlisted slot labels, endpoint modes and broker account IDs.
  The **echo policy** is stated once in `profile/errors.py`: request-supplied
  text (a rejected slot name) is never reflected into rendered prose; field
  names read out of the profiles database are, because naming them is the only
  way an operator can tell which stored row needs repairing. Neither echoes a
  value.
- "Never a length" holds structurally rather than by substring search: the
  availability payload's fields are exactly `{slot, available}` and an error's
  detail is exactly `{reason, message, next_step}`, and both shapes are pinned
  by tests. There is nowhere for a length to be reported.

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
which is exactly the "convert explicitly on load" the contract asks for. Which
fields are the integer ones is **derived** from `LiveEnvelopeValues`' own
annotations, not hand-listed: a hand-listed split would drift silently the next
time a field is added or retyped, and drift there changes the `sha` every
historical arming record is sealed over.

A `live` revision with no envelope at all is refused **in profile vocabulary**,
before settings are constructed. Letting `_enforce_mode_agreement` refuse it
downstream would have rendered *its* message to the operator — a message naming
six environment variables and ending "Refusing to start", which is precisely the
ADR 0059 environment-source rule ADR 0060 supersedes, on a request-time 422.

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

## Retired, and never retired

Package F's cutover, and the distinction it is easiest to get wrong. The
authority is
[`alpaca-configuration-ownership-inventory.md`](../architecture/alpaca-configuration-ownership-inventory.md)
§F; `app/broker_configuration/legacy_environment.py` encodes both lists and
`tests/broker_configuration/test_legacy_environment.py` asserts they stay
disjoint.

**Retired — seven names, refused if present after cutover.** `ALPACA_MODE`,
`ALPACA_LIVE_LOSS_FRACTION`, `ALPACA_LIVE_LOSS_USD`,
`ALPACA_LIVE_SHADOW_SESSIONS`, `ALPACA_LIVE_ARMING_MAX_SESSIONS`,
`ALPACA_LIVE_XH_ENTRY_BPS`, `ALPACA_LIVE_XH_EXIT_BPS`. Once an installation has
a saved profile, `resolve_worker_binding` and `cli_binding.effective_broker`
both refuse with `retired_environment_settings`, naming the variables to delete.
The gate closes and the service still boots (#2014); it is never a crash loop.
Detection reads them exactly as `AlpacaSettings` does — process environment and
`.env`, case-insensitively — so a lower-case spelling that would really reach
`AlpacaSettings` is not missed, and a value that no longer parses is still
detected as present rather than raising on the boot path.

**Never retired.** `ALPACA_API_KEY_ID` and `ALPACA_API_SECRET_KEY` **are** the
`default` slot: refusing a boot because they are present would break every
deployment, and this is the single easiest mistake to make in the cutover.
Neither are `ALPACA_CREDENTIAL_LIVE_*` (the `live` slot), `ALPACA_CLERK_DIR`
(deployment bootstrap), the capability gates
(`ALPACA_SQLITE_MANUAL_TRADING_ENABLED`, `ALPACA_FAULT_INJECTION_ENABLED`,
`ALPACA_PAPER_CARRYOVER_ENABLED`), or the four qualification-container inputs.
`ALPACA_MODE` is the one name on both sides: retired on the runtime path,
**kept** inside `alpaca-clerk-qualification`, which asserts
`test "$ALPACA_MODE" = "paper"` as its paper-only proof, runs
`scripts.run_alpaca_sqlite_qualification` rather than the FastAPI app, and loads
no profile — so the worker's refusal cannot reach it.

Moving an existing deployment across is
`python -m scripts.manage_broker_configuration plan --plan-out …` followed by
`apply` with the printed token; the plan writes nothing, and its token is its
own content hash, so the six numbers cannot change between review and import.

## Read-only account verification and pinning

`verify_account()` observes which broker account a context's credentials and
mode actually reach. It talks to `AccountDiscoveryPort`, a `Protocol` declaring
exactly one method, `get_account`.

The claim "verification performs no mutation" is structural, not a convention —
but only because the **default** port is narrowed too. `AlpacaBroker` implements
the trade port, so handing one straight to a read-only ceremony would have made
this a claim about discipline enforced by an annotation that nothing type-checks
(this repo runs no mypy). `_BrokerAccountDiscovery` wraps it down to the single
read, so the object verification actually holds exposes no `submit` and no
`cancel`. Tests assert that of the default port, and a recording double asserts
nothing but `get_account` was reached for on the injected path.

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

**Both** pin functions enforce it. The independent security review caught the
stronger guard sitting on the weaker gate: `reverify_pinned_account` is what
authorises binding a real account at apply and at startup, and without a
freshness bound a caller replaying a cached verification would have satisfied
the contract's "re-observed at apply and at startup" with a fact of any age. An
observation dated *after* the reading clock is its own refusal, separate from
"too old", because it is a different operator problem: nothing is stale, the
clocks disagree.

`AccountPin.pinned_at_ms` is when the pin was recorded, not when the account was
seen — contract §2.3's field is `account_pinned_at_ms`, and two pins minted from
one observation are two events.

## The interface Package D calls

```python
from app.broker.alpaca.profile import (
    is_known_credential_slot,       # (slot) -> bool                     — no raise, no env read
    require_known_credential_slot,  # (slot) -> str                      — raises CredentialSlotUnknown
    credential_slot_available,      # (slot, *, environment=None) -> bool
    describe_credential_slots,      # (*, environment=None) -> tuple[CredentialSlotAvailability, ...]
    resolve_credentials,            # (slot, *, environment=None) -> ResolvedCredentials
    resolve_runtime_context,        # (*, endpoint_mode, credential_slot, live_envelope=None,
                                    #    account_pin=None, profile_id=None, revision=None,
                                    #    environment=None) -> AlpacaRuntimeContext
    verify_account,                 # async (context, *, discovery=None) -> AccountVerification
    pin_observed_account,           # (verification, *, selected_account_id, existing_pin=None,
                                    #    now_ms=None) -> AccountPin
    reverify_pinned_account,        # (verification, *, pinned_account_id, now_ms=None) -> ObservedAccount
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

A broker and its client are **one** binding: passing both a client and settings
that disagree is refused at construction, because the port would otherwise stamp
its own mode on a snapshot the client fetched from the other mode's endpoint.
`AlpacaTradingClient.bound_settings` exposes what a client is bound to so that
check can be made without reaching into a private attribute.

**Not changed, deliberately:** verification's default path reaches the shared
capture journal, so verifying an unbound draft deposits an account capture
alongside the effective binding's. The capture hook records only method, URL and
body and never auth headers, so this is a provenance question rather than a leak;
journaling every broker response verbatim is the repo's existing audit posture,
and scoping a separate journal for ceremonies is a change Package D or E should
make deliberately if the smear matters.

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
