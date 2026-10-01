# Alpaca credential slots

**Status:** supporting for [ADR 0060](../architecture/adrs/0060-broker-configuration-is-a-user-owned-profile-on-the-clerk-volume.md). Implements [the profile contract](../architecture/broker-configuration-profile-contract.md) §3. The resolver is `app/broker/alpaca/profile/`. Lineage: live.

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

Secret hygiene is asserted by tests, not assumed. Two pre-existing leaks
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
