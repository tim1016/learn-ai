# Alpaca configuration ownership inventory

**Status:** supporting evidence for [ADR 0060](adrs/0060-broker-configuration-is-a-user-owned-profile-on-the-clerk-volume.md) (Package A of the [user-owned broker configurations plan](../design/user-owned-broker-configurations-plan-2026-09-10.md)). Lineage: live.
**Baseline:** every row was read on disk at `037ffe12` on 2026-09-10. Line numbers are from that commit and drift; the classification does not.
**What this is:** the per-setting assignment ADR 0060 Decision 1 defers to. ADR 0060 owns the six classes; this file owns which setting is in which class, who calls it, and whether it migrates. Sections A-D cover the Alpaca path with callers; §E2 classifies the rest of `app/config.py` at class level only, without a caller sweep.

## The six classes

ADR 0060 Decision 1 owns these; they are repeated here only as column headings for the tables below.

| Class | Authority after ADR 0060 |
|---|---|
| **Secret** | Environment injection only. A profile names an opaque credential *slot*, never a value, a variable name, or a URL. |
| **User-owned broker configuration** | The profiles database on the Clerk volume. |
| **User/operator identity** | The durable local-owner record. |
| **Deployment bootstrap** | Environment / `compose.yaml`. Must exist *before* a profile can be loaded. |
| **Capability/release gate** | Code and deployment policy, classified one at a time. Never a profile permission switch. |
| **Code invariant** | Code and accepted decisions. Not configuration at all. |

**Migration disposition** is one of `this delivery` / `later` / `never`. "Never" is a decision, not an omission: ADR 0060 explicitly does not promise that every non-secret becomes user-editable.

## A. Read by `AlpacaSettings` — `PythonDataService/app/broker/alpaca/config.py`

`AlpacaSettings` uses `env_prefix="ALPACA_"`, `case_sensitive=False`, `extra="ignore"` (`config.py:60-65`), so the env name is `ALPACA_` + the upper-cased field name. **`extra="ignore"` is why a retired variable left in `.env` after cutover would be silently dropped.** ADR 0060 open question 1 is now resolved (owner, 2026-09-10): it is *not* dropped silently. A cut-over installation refuses to bind while any retired variable is present, naming the ones to delete — `app/broker_configuration/legacy_environment.py`. The gate closes; the service still boots.

| Env var | Declared | Type and constraints | Class | Migrates | Principal callers |
|---|---|---|---|---|---|
| `ALPACA_API_KEY_ID` | `config.py:67` | `str`, required, `min_length=1` | Secret | never (a profile references a slot) | `client.py:151`; `trade_updates.py:892`; `market_liveness.py:356`; `scripts/hitl_alpaca_capture.py:362` |
| `ALPACA_API_SECRET_KEY` | `config.py:68` | `str`, required, `min_length=1` | Secret | never (slot) | same four sites as above |
| `ALPACA_MODE` | `config.py:73` | `Literal["paper","live"]`, default `"paper"` | User-owned broker configuration — **with a retained deployment/test use** | this delivery for the runtime path; **never** for the qualification container | `client.py:153` (`is_paper` → SDK endpoint); `trade_updates.py:927` (stream URL from `base_url`); `broker.py:85` (capability set); `adapter.py:191` (mode-vs-account-number disagreement); `fault_injection.py:87`; `clerk/live_arming_ceremony.py:397`; `services/alpaca_live_verdict.py:298,400,421,441`; `main.py:271`; `scripts/manage_alpaca_sqlite_clerk.py:199,269`; `scripts/hitl_alpaca_capture.py:360` |
| `ALPACA_CLERK_DIR` | `config.py:74` | `Path`, default `<PythonDataService>/artifacts/alpaca_clerk` | Deployment bootstrap | never | `main.py:197,277`; `routers/brokers.py:737,745`; `symbol_validity.py:230`; `services/live_arming_admission.py:62`; `services/broker_v2_panel/evidence_service.py:225` (`clerk/sqlite/repository_lifecycle.py:189` only *names* the variable in an unsupported-filesystem message; it reads nothing) |
| `ALPACA_LIVE_LOSS_FRACTION` | `config.py:81` | `float \| None`, `gt=0, lt=1, allow_inf_nan=False` | User-owned broker configuration | this delivery | envelope construction (below) |
| `ALPACA_LIVE_LOSS_USD` | `config.py:82` | `float \| None`, `gt=0, allow_inf_nan=False` | User-owned broker configuration | this delivery | envelope construction |
| `ALPACA_LIVE_SHADOW_SESSIONS` | `config.py:83` | `int \| None`, `ge=1` | User-owned broker configuration | this delivery | envelope construction; also `scripts/manage_alpaca_shadow.py:166` as the fallback for `--required-sessions` |
| `ALPACA_LIVE_ARMING_MAX_SESSIONS` | `config.py:84` | `int \| None`, `ge=1` | User-owned broker configuration | this delivery | envelope construction; the lapse count sealed on the arming record |
| `ALPACA_LIVE_XH_ENTRY_BPS` | `config.py:89` | `float \| None`, `ge=0, lt=10_000, allow_inf_nan=False` | User-owned broker configuration | this delivery | envelope construction; `marketable_limit.py:93-98,118`; operator prose `clerk/program_leg.py:122` |
| `ALPACA_LIVE_XH_EXIT_BPS` | `config.py:90` | `float \| None`, `ge=0, lt=10_000, allow_inf_nan=False` | User-owned broker configuration | this delivery | envelope construction; `marketable_limit.py`; `clerk/program_leg.py:141` |

**Shared callers of the six live values:** `clerk/live_envelope.py:56-63,79-86` (`_SETTINGS_FIELDS`, `LiveEnvelopeValues.from_settings`) — the single construction site; `main.py:268-280` (read once at startup, handed to `select_active_clerk_runtime`); `clerk/live_arming_ceremony.py:388-403` (`configured_envelope`); `clerk/shadow_authority.py:68,82,182`; `clerk/live_authority.py:123`; `clerk/sqlite/envelope_admission.py:61`; `services/live_arming_admission.py:84,87`.

**Cross-field gate that must move with them:** `_enforce_mode_agreement` (`config.py:92-107`) refuses to start when `mode == "live"` and any of the six is `None`, naming the missing variables. Its equivalent in the profiles world is "a live revision is incomplete and cannot be applied", not a startup crash — see ADR 0060 Decision 7.

**Derived, never configurable (code invariant):** `base_url` (`config.py:117-120`) maps mode → `https://paper-api.alpaca.markets` / `https://api.alpaca.markets` (`_BASE_URL_BY_MODE`, `config.py:32-35`), plus `is_paper` / `is_live`. A profile must never supply an API base URL; the endpoint stays derived from mode.

### Type-fidelity warning — read this before designing the storage

`LiveEnvelopeValues` (`app/broker/alpaca/clerk/live_envelope.py:70-93`) is a frozen dataclass with **no field validation of its own**. Every range check above lives *only* in `AlpacaSettings`. Its `sha` is `canonical_sha256(asdict(self))`, and `canonical_sha256` (`clerk/sealed_ledger.py:31-36`) is:

```
sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
```

There is no `default=` on that `json.dumps`. Consequences that a storage layer can break silently, and that every already-sealed arming record depends on:

- A `decimal.Decimal` **raises `TypeError`** rather than serializing. Never store or return these as `NUMERIC`/`Decimal`.
- Floats use Python's shortest repr, so **`5000` and `5000.0` hash differently**. The four float fields must round-trip as `float`.
- `shadow_sessions` and `arming_max_sessions` must round-trip as exactly `int` — not `1.0`, not `True`. `live_arming.py`'s `_is_int` guard exists precisely because the dataclass cannot enforce it.

That is the evidence. The rule it produces — `REAL` / `INTEGER` columns, explicit conversion on load, `NUMERIC` and `Decimal` banned, and a test pinning `sha` equality on every read path — is [ADR 0060](adrs/0060-broker-configuration-is-a-user-owned-profile-on-the-clerk-volume.md) Decision 6, with the per-field table in the [contract doc](broker-configuration-profile-contract.md) §2.4. It is not restated here, so it cannot drift.

## B. Read by the application `Settings` — `PythonDataService/app/config.py`

`Settings` has **no** env prefix, `case_sensitive=True`, `extra="ignore"` (`config.py:22-26`).

| Env var | Declared | Type | Class | Migrates | Callers |
|---|---|---|---|---|---|
| `ALPACA_SQLITE_MANUAL_TRADING_ENABLED` | `config.py:69` (default `False`) | `bool` | Capability/release gate | **never** — a profile may not enable a deferred capability | `clerk/sqlite/runtime.py:336,409,433` (`manual_trading_enabled=`). Manual live orders remain out of scope (ADR 0059 Decision 11), so this gate never becomes a live permission. |
| `ALPACA_PAPER_CARRYOVER_ENABLED` | `config.py:72` (default `False`) | `bool` | Capability/release gate | **never** — and see below | **No runtime consumer.** Verified: the only non-declaration reference in the repo is `tests/broker/v2panel/test_deploy_scoped_route.py:794`. The value it would feed, `carryover_account_policy_enabled`, comes from `BotRunner._carryover_allowed`, hardcoded `False` at `app/services/bot_runner.py:294` and never reassigned (`:351,440,490,1783` read it). ADR 0030:370 still describes it as live. **Disposition: this is dead configuration — retire it or wire it deliberately, in its own change. It must not be migrated into a profile, because migrating a flag that controls nothing would manufacture a user-facing switch with no behaviour behind it.** |
| `ALPACA_FAULT_INJECTION_ENABLED` | `config.py:78` (default `False`) | `bool` | Capability/release gate (dev-only) | **never** | `main.py:782` conditionally registers `routers/alpaca_fault_injection.py` (404 when off), warning at `:789`; `broker/alpaca/fault_injection.py:84-92` (`injection_permitted()` = flag **and** paper posture, fail-closed) and `:145`. Written — not read — by `scripts/export_openapi_contract.py:52`, which forces `"false"` so the committed OpenAPI contract is deterministic. ADR 0059 Decision 10 keeps it paper-gated. |

## C. Operator identity

| Env var | Declared | Type | Class | Migrates | Callers |
|---|---|---|---|---|---|
| `PANEL_OPERATOR_IDENTITY` | `app/config.py:65` (default `"operator"`) | `str` | User/operator identity | this delivery — becomes the durable local-owner record's display label | `routers/brokers.py:504,529,565,615,648` (`operator_id=`); `routers/broker_v2_panel.py:299,555,626,664,793` (`actor=` / `operator_identity=`). `broker_v2_panel.py:15` documents that operator identity is never a request field — ADR 0060 Decision 3 keeps that property and gives it a durable record. Journal actor strings already written are preserved as written. |

## D. Deployment bootstrap consumed only by `compose.yaml`

All four belong to the `alpaca-clerk-qualification` service (`compose.yaml:272`, `profiles: ["qualification"]`, `read_only: true`, `network_mode: none`). None is read by Python.

| Env var | compose.yaml | Class | Migrates | Role |
|---|---|---|---|---|
| `ALPACA_CLERK_PRODUCTION_ACCOUNT_ID` | `:283`, `:314` (`:?` hard-fail), `:329` | Deployment bootstrap | never | Passed as `--production-account-id` to `scripts.run_alpaca_sqlite_qualification`. |
| `ALPACA_QUALIFICATION_API_KEY_ID` | `:287`, `:317`, `:321` | Secret | never | Exported *as* `ALPACA_API_KEY_ID` inside the container so production credentials never enter it. |
| `ALPACA_QUALIFICATION_API_SECRET_KEY` | `:288`, `:318`, `:322` | Secret | never | Same, for the secret half. |
| `ALPACA_CLERK_UI_EVIDENCE_PATH` | `:303` | Deployment bootstrap | never | Host-side bind-mount source for the pre-verified UI evidence file; default is the committed audit JSON. Asserted in `tests/scripts/test_run_alpaca_sqlite_qualification.py:763`. |

The same service hardcodes `ALPACA_FAULT_INJECTION_ENABLED=true` (`:286`) and guards `test "$${ALPACA_MODE}" = "paper"` (`:320`). **That guard is why `ALPACA_MODE` cannot simply be deleted from the environment**: it is the qualification container's paper-only proof, and it stays until an equivalent proof replaces it.

`ALPACA_CLERK_DIR` never appears in `compose.yaml`. Compose instead masks the *default* path with the external named volume `alpaca-clerk-data` → `learn-ai-alpaca-clerk-data` at `/app/artifacts/alpaca_clerk` (`compose.yaml:126`, volume declaration in the `volumes:` block), for the macOS virtiofs/WAL reason documented at `:120-125`. **This is the volume ADR 0060 puts the profiles database on.** The `python-service` service sets no `ALPACA_*` entries at all; it loads `env_file: ./PythonDataService/.env` (`:80-82`), and the comment at `:76-79` records that the running data plane reads only that file while the repo-root `.env` feeds compose interpolation.

## E. Names that appear in docs but are not settings

Recorded so a future sweep does not migrate something that does not exist.

| Name | Where | Status at `037ffe12` |
|---|---|---|
| `ALPACA_MARKET_STATUS_UPSTREAM_URL` | the plan, §2 and §6 | **Not present anywhere in code, `compose.yaml`, or either `.env.example`** — independently confirmed by grep. It is an uncommitted rehearsal edit on another branch, described by the plan itself as provisional evidence outside its baseline. Classify it when it lands: the *choice* to use a shared status feed is user-owned; the *address* is deployment bootstrap, and it must never become a free-form credential-bearing URL input. |
| `ALPACA_MARKET_ENDPOINT` | `docs/audits/alpaca-paper-live-workflow-2026-09-09.md:274` | Present in some operator's untracked env file with **no** application or script consumer. The endpoint is derived from `ALPACA_MODE`. A leftover of exactly the kind open question 1 is about. |
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | `docs/superpowers/plans/2026-09-08-live-slice-5-risk-envelope.md:150-151,161` | Stale plan-doc names that never shipped; the real fields are `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY`. `extra="ignore"` would drop them silently. |

Matches of `ALPACA_[A-Z_]+` that are **Python or TypeScript identifiers, not environment variables**: `ALPACA_EXTENDED_HOURS_WINDOW`, `ALPACA_PAPER_CAPABILITIES`, `ALPACA_LIVE_CAPABILITIES` (`broker/alpaca/broker.py:37,44,66`); `ALPACA_TRADE_UPDATE_EVENTS` (`adapter.py:373`); the `ALPACA_STATUS_*` reason codes (`market_liveness.py:251,255,259`); `ALPACA_ACCOUNT_NOT_TRADABLE` / `ALPACA_PAPER_DEPLOY_READY` / `ALPACA_DRY_RUN_READY` (`services/broker_v2_panel/paper_deploy_service.py:671,686,744`); `ALPACA_PORTFOLIO_HISTORY_CHART_FACTORY` (an Angular `InjectionToken`). `Backend/`, `Backend.Tests/` and `.github/` contain no `ALPACA_` references at all.

## E2. The rest of `app/config.py` — class only, no caller sweep

The plan asks Package A to inventory the other application settings so the six classes are shown to cover the whole surface, while explicitly not promising that every non-secret becomes user-editable. `app/config.py` declares 25 settings; four are covered above. **The remaining 21 are classified here at class level only — no caller sweep was performed, and no disposition below is authority for a migration.** ADR 0060 decides authority for the Alpaca path alone; anything marked *later (candidate)* needs its own decision, with its own caller sweep, before it moves.

| Setting | Line | Class | Disposition |
|---|---|---|---|
| `POLYGON_API_KEY` | `:29` | Secret | never |
| `FRED_API_KEY` | `:37` | Secret | never |
| `DATA_PLANE_CONTROL_SECRET` | `:59` | Secret | never |
| `POSTGRES_URL` | `:120` | Secret (connection string) | never |
| `LEAN_LAUNCHER_TOKEN` | `:154` | Secret | never |
| `HOST`, `PORT` | `:40-41` | Deployment bootstrap | never |
| `ALLOWED_ORIGINS` | `:51` | Deployment bootstrap | never |
| `TRUSTED_HOSTS` | `:79` | Deployment bootstrap | never |
| `BACKEND_URL` | `:94` | Deployment bootstrap (service discovery) | never |
| `LEAN_LAUNCHER_URL` | `:153` | Deployment bootstrap (service discovery) | never |
| `LEAN_DATA_WRITE_ROOT` | `:139` | Deployment bootstrap (mounted root) | never |
| `DATA_LAKE_ROOT_ID` | `:146` | Deployment bootstrap (infrastructure identity) | never |
| `GIT_COMMIT_SHA` | `:48` | Deployment bootstrap (build stamp) | never |
| `MAX_REQUESTS_PER_MINUTE` | `:97` | Deployment bootstrap (service admission limit) | never — the plan classes admission limits separately from user preferences |
| `DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL` | `:60` | Capability/release gate (security) | never |
| `CLERK_TRANSACTION_PROJECTION_ENABLED` | `:127` | Capability/release gate | never |
| `SIGNAL_PROGRAM_WIRING_DIGEST_ENFORCED` | `:135` | Capability/release gate (rollout) | never |
| `POLYGON_RATE_LIMIT_PER_MIN` | `:34` | Provider entitlement / throttle preference | **later (candidate)** — the plan names these as possibly meriting a subsequent profile |
| `MAX_NULL_PERCENTAGE` | `:89` | User data-processing choice | **later (candidate)** |
| `REMOVE_DUPLICATES` | `:90` | User data-processing choice | **later (candidate)** |
| `FILL_METHOD` | `:91` | User data-processing choice | **later (candidate)** |

Nothing in this table migrates in this delivery.

## F. Migration summary

| Disposition | Settings |
|---|---|
| **This delivery** | the six `ALPACA_LIVE_*` values; `ALPACA_MODE` as a per-revision endpoint mode (runtime path only); `PANEL_OPERATOR_IDENTITY` as the local-owner display label; the credential *slot reference* that replaces the direct key/secret read |
| **Later** | `ALPACA_MARKET_STATUS_UPSTREAM_URL` once it lands (choice user-owned, address deployment); the four *later (candidate)* rows in §E2, each needing its own decision and caller sweep |
| **Never** | `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY`, `ALPACA_QUALIFICATION_API_KEY_ID`, `ALPACA_QUALIFICATION_API_SECRET_KEY` (secrets); `ALPACA_CLERK_DIR`, `ALPACA_CLERK_PRODUCTION_ACCOUNT_ID`, `ALPACA_CLERK_UI_EVIDENCE_PATH` (deployment bootstrap); `ALPACA_MODE` inside the qualification container (paper-only proof); `ALPACA_SQLITE_MANUAL_TRADING_ENABLED`, `ALPACA_FAULT_INJECTION_ENABLED` (capability gates); `ALPACA_PAPER_CARRYOVER_ENABLED` (dead — retire or wire, separately); `base_url` / `is_paper` / `is_live` (derived code invariants) |

**Cutover status (package F, 2026-09-10).** The "this delivery" row is built. The seven runtime-path names — `ALPACA_MODE` and the six `ALPACA_LIVE_*` values — are encoded as `RETIRED_SETTINGS` in `app/broker_configuration/legacy_environment.py`, and everything in the "Never" column that could plausibly be mistaken for one is encoded beside it as `NEVER_RETIRED_SETTINGS`, with a test asserting the two stay disjoint. `scripts/manage_broker_configuration.py` imports an existing deployment's values into a profile under the repo's plan/apply confirmation ceremony. `PANEL_OPERATOR_IDENTITY` migrates by *seeding* the local-owner record on first read and is **not** retired: it keeps its deployment-bootstrap role for an installation that has not created an owner yet, and an existing owner label is never overwritten.

**Ambiguity rule.** After cutover no user setting is read from both the profiles database and the environment. A setting is in exactly one column above. `ALPACA_MODE` is the one name that appears twice, and it is not ambiguous: the runtime path reads the effective revision's endpoint mode, and the qualification container — a separate, network-isolated, read-only container that never loads a profile — keeps the variable as its own assertion.
