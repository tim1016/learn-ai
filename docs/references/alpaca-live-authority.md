# Alpaca live authority — graduation, the admission chain, and the first real order

**Status:** canonical for ADR 0059 slice 7 (2026-09-09). Lineage: live.

## What it is

The **Live Account Authority** is the same `sqlite` Clerk the paper account
runs, composed over a real-money account with the real trade port and the two
gates a real-money ENTER is admitted against: the risk envelope (ADR 0059 D4,
[alpaca-live-envelope](alpaca-live-envelope.md)) and the per-instance arming
gate (D3/D11, [alpaca-live-arming](alpaca-live-arming.md)). It exists only on
three-way mode agreement (D1): the configured mode, the broker-observed
account, and the cutover's activation record naming that exact account. This
is the slice after which a real order is possible, and this note says exactly
when one is.

## Graduation

A live account is **shadowed or live-custodied, never both at once** (design
R1): one primary authority per process. On a live-mode boot,
`active_authority.select_active_clerk_runtime` asks the cutover's
`ActivationStore` (`accounts/alpaca/`) for a record naming the observed
account. Present, `live_authority.select_live_clerk_runtime` composes the live
authority; absent, the Shadow Account Authority is composed exactly as slice 4
built it. Graduation is therefore the live cutover:

```bash
cd PythonDataService
# The evidence file names the account, `"account_mode": "live"`, flat
# positions and no open orders; the CLI refuses live evidence unless
# ALPACA_MODE=live. No shadow receipt is required to graduate: shadow is a
# mode, not a requirement (owner decision 2026-09-09).
python -m scripts.manage_alpaca_sqlite_clerk --account-id <LIVE> --artifacts-root <CLERK_DIR> \
    cutover-initialize --broker-evidence live-evidence.json --max-evidence-age-ms 600000 --runner-artifacts-root <RUNNER_ROOT>
python -m scripts.manage_alpaca_sqlite_clerk ... cutover-plan  --output plan.json ...
python -m scripts.manage_alpaca_sqlite_clerk ... cutover-apply --plan plan.json --confirmation-token <token> ...
```

A never-legacy live account has no legacy JSONL authority to quarantine; live
evidence by itself permits the empty legacy set (R3) — no shadow receipt is
consulted, because shadow is a mode, not a requirement (owner decision
2026-09-09). The account must be **flat and order-free** to graduate — nothing
of ours has ever submitted on it, so any position is a human's; flatten it by
hand first. Stop the shadow instances
before the cutover: after it they are sealed on `shadow:<id>` while the
authority custodies `<id>`, so they are foreign to it — repaired from their own
lifecycle files at boot, refused `SEALED_ACCOUNT_MISMATCH` on any Start (R15).
Graduation has no reversal in this slice (`dev_reset` refuses live by D10); a
`LiveDeactivationRecord` mirroring disarm is the follow-up.

Cold start on the live authority is the paper path's (R18): the cutover's
flat-and-order-free evidence and `recover()`'s reconciliation of open orders
at every boot. The shadow namespace scan is not applied — after the first real
order it would refuse every boot.

### After graduation: the rehearsal's bindings are foreign, not corrupt (R15)

A binding sealed on a custody account the installed primary authority does
not custody — after graduation, the rehearsal's `shadow:<live_account_id>`
bindings under the live primary — is *foreign* to that authority, not
corrupt. `bot_boot_recovery.py` decides that from the registry's own routing
(the binding's authority is the primary lifecycle authority) plus the same
comparison Start makes (`binding.sealed_account_id != <installed Clerk
account id>`), so a `sim:` Dry Run binding — routed to its own per-instance
authority — is never foreign. Such a binding is named in the boot report's
`foreign_instances`, logged once (`action=boot_recovery_foreign_binding`),
and **left exactly as its own lifecycle files say**: never projected against
an authority that has never seen it, no duty state authored from the sweep
(ADR 0050), and the sweep never aborts. Unlike
`authority_unavailable_instances` this does not close the Start gate —
foreign bindings are refused one at a time on Start with
`SEALED_ACCOUNT_MISMATCH` (`run_admission.py`), and native instances stay
startable. The live verdict's arming count reads the same rule: a
`shadow:`-sealed instance is not armed on the graduated account.

## Where it runs

- `PythonDataService/app/broker/alpaca/clerk/live_authority.py::select_live_clerk_runtime`
  — the boot story. Refuses, as a typed `unavailable` runtime and never an
  aborted data plane: `LIVE_CONTROL_UNAUTHENTICATED`
  (`DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL=true`, R14), `LIVE_MODE_DISAGREEMENT`,
  `LIVE_ENVELOPE_MISSING`, `ACTIVATION_RECORD_INVALID`, `SQLITE_CLERK_STARTUP_FAILED`.
- `app/broker/alpaca/clerk/live_arming_gate.py` — `ArmingSnapshot` and
  `ArmingGate`, the per-instance cache one ledger read fills.
- `app/broker/alpaca/clerk/sqlite/arming_admission.py::require_arming_admission`
  — the third ENTER admission.
- `app/broker/alpaca/clerk/sqlite/live_envelope_sync.py` — every 15 s, one
  ledger read seals the envelope and publishes the snapshot; the runner's
  sealed bindings arrive through an injected callable (`instance_seals`).
- `app/services/live_arming_admission.py` — the arming fact Start and Resume read.

## The ENTER admission chain

`accept_enter` runs, inside the custody fence and before `ENTER_ACCEPTED`:

1. `require_admission` — holds and uncertainties, the loss hold included;
2. `require_arming_admission` — the instance must be `armed` **right now**
   (the snapshot is at most 45 s old; the status is derived at the admission
   instant, so a lapse at the ET-date boundary is enforced at the instant);
3. `require_envelope_admission` — the cash bound.

Arming runs before the envelope so an unarmed instance never reserves cash.
EXIT is never subject to 2 or 3. Refusals from 2, each a rejected receipt on
the decision:

| Code | When |
|---|---|
| `LIVE_ARMING_LEDGER_INVALID` | the last refresh could not verify the ledger |
| `LIVE_MODE_DISAGREEMENT` | the broker's mode stopped agreeing mid-session (R2) |
| `LIVE_ARMING_UNOBSERVED` | no snapshot, or one older than 45 s |
| `LIVE_ARMING_REQUIRED` | the ledger has never named this instance |
| `LIVE_ARMING_LAPSED`, `LIVE_ARMING_REVOKED`, `LIVE_ARMING_SEAL_CHANGED`, `LIVE_ARMING_FUTURE_DATED`, `LIVE_ENVELOPE_DISAGREEMENT` | the instance's own state (slice 6) |

All of them are transient (retry on the next decision clock); the reaction to
a lost arming is below, never a fatal halt.

## Deploy, arm, trade

A strategy instance is immutable per account and its account is inside its
seal, so the instance that rehearsed as `shadow:<id>` cannot be the instance
that trades on `<id>`: the live run is a **new instance** (R6).

1. Deploy it on the live world (`execution_mode: "live"`, offered only there).
   The launch is admitted running; every ENTER it makes refuses
   `LIVE_ARMING_REQUIRED` until it is armed, and the admitted decision and the
   receipt say so.
2. Arm it: `manage_alpaca_arming plan` / `apply`. The ceremony reads the
   live-sealed binding, records the instance's own current shadow receipt
   when it has one (`shadow_receipt_sha256` is null otherwise), and arms
   either way — shadow is a mode, not a requirement (R7; owner decision
   2026-09-09).
3. Within one sync tick the gate holds the instance `armed`; its next ENTER
   passes all three admissions and `submit_enter` hands the leg to the real
   trade port.

## The halt

Decision 8's `desired_state = PAUSED` is deliberately **not** written (R10):
`PAUSED` is observe-only for EXIT too, and would strand a real position every
morning under `ALPACA_LIVE_ARMING_MAX_SESSIONS=1`. What is built: new
submission stops (every ENTER of an instance that is no longer armed refuses,
as a rejected receipt carrying **that instance's own arming code** —
`LIVE_ARMING_LAPSED`, `LIVE_ARMING_REVOKED`, `LIVE_ARMING_SEAL_CHANGED`,
`LIVE_ARMING_FUTURE_DATED` or `LIVE_MODE_DISAGREEMENT`; no receipt ever
carries `LIVE_VERDICT_TRANSITION_HALT`); it is loud (the sync logs
`live_verdict_transition_halt` at warning level once per instance per
transition, naming the code the receipt will carry, and the verdict names the
instance); and
resumption is guarded by the arming ceremony itself, after which the next tick
admits. EXITs keep running; the operator's reduce-only actions
(`execute_safe_flatten`, the cohort flatten) are EXIT-shaped and never gated
(R17). The manual order ticket stays paper-only (D11).

## The thirteen gates, re-meant

See the table in design R8: `manual_order_runtime` keeps `LIVE_ACCOUNT_REFUSED`;
`historical_execution_recovery` still refuses `LIVE_ACCOUNT_REFUSED` — the one
paper-only gate this slice does not re-mean, a named follow-up; `cutover`
admits `paper | live`; `dev_reset` still refuses non-paper on the configured
mode; `panel_deploy` offers `live` on the live world; the `run_admission`
corpus gate is unchanged and the arming fact sits beside it; `CustodyWorld`
gains `real_live` and every world admits exactly one account mode.

## In the live verdict

`observe_arming` and `observe_loss_hold` read on every live-custodying facade
authority (shadow, or sqlite on `real_live`); the paper authority reads
neither. `observe_arming` counts only live-sealed instances on the
`real_live` world — the rehearsal's `shadow:`-sealed bindings are foreign
to a graduated authority (R15) and are never counted as armed under it. On the
live authority the headline is `LIVE account <id> — N instances armed,
real-money submission open` or `… — real-money authority installed, no
instance armed`; `clerk_authority` stays `sqlite` (`configured_mode` names the
world).

## Operator configuration

**The endpoint mode and the six risk-envelope values are a saved broker profile,
not environment settings** (ADR 0060, superseding ADR 0059's environment-source
rule). They are edited under `/api/brokers/alpaca/configuration`, staged, and
made effective by pressing Apply and performing a controlled restart. There is
no `ACTIVE_PROFILE` variable and no `ALPACA_MODE` on the runtime path.

Once an installation has a saved profile, leaving any of the seven retired
variables in place — `ALPACA_MODE`, `ALPACA_LIVE_LOSS_FRACTION`,
`ALPACA_LIVE_LOSS_USD`, `ALPACA_LIVE_SHADOW_SESSIONS`,
`ALPACA_LIVE_ARMING_MAX_SESSIONS`, `ALPACA_LIVE_XH_ENTRY_BPS`,
`ALPACA_LIVE_XH_EXIT_BPS` — is answered by naming the ones to delete
(`retired_environment_settings`). **A deliberate change refuses; an ordinary
restart does not.** The worker binding a staged revision an Apply named refuses
and the gate closes (the service still boots), as does any operator CLI. A
restart of an already-effective configuration **binds and logs an error naming
the variables on every boot** instead, because a worker left with no broker
cannot place an EXIT (ADR 0060 D4.3) and the stale values are not read by
anything it binds. `scripts/manage_broker_configuration.py` moves an existing
deployment across; the per-boot table is in
[`alpaca-credential-slots.md`](alpaca-credential-slots.md#retired-and-never-retired).

What **remains** environment-injected, and always will: the credential pairs
(`ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY` are the `default` slot;
`ALPACA_CREDENTIAL_LIVE_*` are the `live` slot), `ALPACA_CLERK_DIR`, and the
capability gates. The data plane reads **only** `PythonDataService/.env` (compose
`env_file`); the repo-root `.env` feeds compose interpolation only. On a live
boot, `DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL=false` and
`ALPACA_FAULT_INJECTION_ENABLED=false` (the first refuses to install the
authority; the second is already refused by `injection_permitted`).

## Residuals

- One primary authority per process (R1): a graduated account cannot shadow
  a new instance until a secondary-authority seam (a shadow facade per
  `shadow:` binding beside the live primary, the way `sim:` is selected per
  instance) lands — a named follow-up slice, deferred by the owner on
  2026-09-09. Rehearsal is optional, so this blocks nothing.
- Graduation has no reversal (R1).
- The arming gate is a 15 s cache of local evidence (R5).
- The halt writes no desired state (R10) — owner question E1.
- A refused live boot labels its compat surfaces "paper" (`custody_world_or_paper`).
- A human trading the live account withdraws day P&L to unknown for the day
  (slice 5 R5), so every program ENTER refuses `LIVE_ENVELOPE_UNOBSERVED` — E7.
- `StrategySpec.submit_mode` is not stamped at deploy — E8.
- **Historical execution recovery stays paper-only.** `historical_execution_recovery.py`
  refuses `LIVE_ACCOUNT_REFUSED` at both entry points; re-meaning it for a live
  account (its own admission and a live-safe replay) is a follow-up slice.

## Decision record

[ADR 0059](../architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md)
Decisions 1, 3, 8, 10, 11 and Consequence 7. Controller rulings R1–R18 are in
`docs/superpowers/specs/2026-09-09-live-slice-7-gate-remeaning-design.md`.
Predecessors: [alpaca-shadow-authority](alpaca-shadow-authority.md),
[alpaca-live-envelope](alpaca-live-envelope.md), [alpaca-live-arming](alpaca-live-arming.md).
