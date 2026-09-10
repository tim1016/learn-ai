# Alpaca live arming — the ceremony, the ledger, and the lapse

**Status:** canonical for ADR 0059 slice 6 (2026-09-09). Lineage: live.

## What it is

Real-money submission is permitted per **sealed instance**, never per account
and never by configuration. The permission is a supervised ceremony modelled on
the SQLite cutover: `plan` is read-only and re-observes every input, `apply`
re-observes again, accepts no force mode, and honours the same confirmation
window (120 s default, 300 s maximum). What `apply` writes is one sha256-sealed
`LiveArmingRecord` on an append-only, account-rooted ledger.

An arming is bound to what it named. A change to the instance's sealed program,
to any `ALPACA_LIVE_*` value, or to the account disarms it; re-arming is the
same ceremony run again. An arming also **lapses** after
`ALPACA_LIVE_ARMING_MAX_SESSIONS` calendar NYSE trading sessions — a deliberate
"come back and look" fence, because an armed bot nobody has looked at in a
month is the configuration accident this ADR was written to prevent.

**Since ADR 0059 slice 7 an arming record is what admits a real-money ENTER**
on the live authority: `require_arming_admission` reads it at every ENTER,
between the holds and the envelope, through the `ArmingGate` the sync
refreshes each tick. The CLI's `submission_admitted` field now reports
whether a live authority exists for the account; its `note` says under which
authority the record is read. See
[alpaca-live-authority](alpaca-live-authority.md).

## Where it runs

- `PythonDataService/app/broker/alpaca/clerk/live_arming.py` — the two records,
  their sealing and verification, the reason codes, and the pure status rule
  (`arming_status`, `sessions_used`). No I/O, no clock, no broker.
- `PythonDataService/app/broker/alpaca/clerk/live_arming_ledger.py::LiveArmingLedger`
  — the append-only ledger at
  `<clerk_dir>/accounts/arming/<live_account_id>/live_arming.jsonl`, written
  under the advisory file lock with the same `sealed_ledger.py` discipline the
  shadow receipts use. Its own tree, deliberately: not `accounts/alpaca/<id>/`
  and not inside a custody namespace directory, so no custody-detection path
  (cutover initialization, the latent-database checks) can mistake an arming
  ledger for an authority. `revoke_latest` reads the instance's latest row and
  appends the revocation under **one** lock acquisition, so a concurrent
  re-arm cannot make `revokes_record_sha256` name a stale record;
  `discover` finds the ledger naming an instance without the activation proof
  (see Residuals).
- `PythonDataService/app/broker/alpaca/clerk/live_arming_ceremony.py` —
  `observe_arming_inputs`, `plan_arming`, `apply_arming`, `disarm`,
  `account_arming`.
- `PythonDataService/app/broker/alpaca/clerk/ceremony.py` — the plan token, the
  TTL bounds and the three confirmation checks, shared with
  `clerk/sqlite/cutover.py`, which invented the shape.
- `PythonDataService/scripts/manage_alpaca_arming.py` — the operator entry
  point, and the only writer.
- `PythonDataService/app/broker/alpaca/clerk/sqlite/live_envelope_sync.py::LiveEnvelopeSync._refresh_sealed_envelope`
  — re-reads the ledger on every 15 s tick and assigns `LiveEnvelopeGate.sealed`
  from the newest arming record's own `envelope_values`.
- `PythonDataService/app/services/alpaca_live_verdict.py::observe_arming` — the
  verdict's `armed_instance_count` and `envelope_state`.

## The four inputs

`plan` reads each of these, and `apply` reads every one of them again and
refuses if any has moved (`LIVE_ARMING_INPUTS_CHANGED`). The ceremony never
contacts the broker: mode agreement against Alpaca stays the runtime's job at
boot, and the live authority's at admission (`LIVE_MODE_DISAGREEMENT` from the
sync's read).

| Input | Where it comes from | Refusal if absent |
|---|---|---|
| Settings | `ALPACA_MODE=live` and a complete `LiveEnvelopeValues.from_settings` | `LIVE_ENVELOPE_MISSING` |
| The live account id | the shadow activation fence under the artifacts root — **observed, never supplied**, so an arming cannot name an account no shadow gate was run against | `LIVE_ARMING_INSTANCE_UNSEALED` |
| The instance's sealed binding on that account | `BotBindingRepository.list_for_broker("alpaca")`, filtered to `sealed_account_id` in `{<live_account_id>, shadow:<live_account_id>}` | `LIVE_ARMING_INSTANCE_UNSEALED` |
| A current shadow receipt | `ShadowReceiptStore.current(sid, configured_signal_hash=…, required_sessions=ALPACA_LIVE_SHADOW_SESSIONS)` | *(none — recorded when present, null otherwise; owner decision 2026-09-09)* |

Both custody ids are admissible for the same account because shadow custody
seals `shadow:<live_account_id>` while the live authority seals
the live id itself. They are one account to an operator.

## The record

```json
{"kind":"armed","schema_version":1,"live_account_id":"9LIVE0001",
 "strategy_instance_id":"ema-shadow-1","seal_hash":"<64 hex>",
 "configured_signal_hash":"<64 hex>","shadow_receipt_sha256":"<64 hex>",
 "envelope_values":{"arming_max_sessions":20,"loss_fraction":0.05,"loss_usd":5000.0,
                    "shadow_sessions":3,"xh_entry_bps":10.0,"xh_exit_bps":10.0},
 "envelope_sha256":"<64 hex>","armed_at_ms":1789394400000,"max_sessions":20,
 "record_sha256":"<64 hex>"}
```

`record_sha256` is the canonical sha256 of every other field. A reader
recomputes it *and* checks that `envelope_sha256` is the sha of the
`envelope_values` beside it; either mismatch is `LiveArmingInvalid` and the
whole ledger refuses to answer.

`seal_hash` is the instance's **whole** sealed-program hash
(`SealedBotProgram.bot_configuration_hash`), not the signal-only
`configured_signal_hash`. Arming binds to what will trade — size, action plan
and account included — so changing the size alone disarms the instance.
`configured_signal_hash` is carried beside it because that is what the shadow
receipt binds to.

A revocation is the same file, one line, `"kind": "disarmed"`:

```json
{"kind":"disarmed","schema_version":1,"live_account_id":"9LIVE0001",
 "strategy_instance_id":"ema-shadow-1","revokes_record_sha256":"<64 hex>",
 "disarmed_at_ms":1789394405000,"record_sha256":"<64 hex>"}
```

## States and codes

The instance's **latest** row decides, and the checks run in this order. The
first failure names the state, which is why an instance whose seal changed
*and* whose sessions are spent reports the seal: re-sealing is what its
operator has to do first. `LIVE_ARMING_FUTURE_DATED` is checked as a plain
comparison against `now_ms`, before `sessions_used` is ever called, so a
record dated after the caller's clock can still be reported as seal-changed or
in envelope disagreement without the calendar ever seeing a reversed range.

| Order | Condition | State | `reason_code` |
|---|---|---|---|
| — | no row for this instance | `unarmed` | *(none)* |
| 1 | the latest row is a revocation | `disarmed` | `LIVE_ARMING_REVOKED` |
| 2 | `seal_hash` differs from the instance's current sealed-program hash (an absent binding counts as different) | `disarmed` | `LIVE_ARMING_SEAL_CHANGED` |
| 3 | `envelope_sha256` differs from the configured envelope's sha | `disarmed` | `LIVE_ENVELOPE_DISAGREEMENT` |
| 4 | `now_ms` precedes the record's `armed_at_ms` — a rolled-back clock | `disarmed` | `LIVE_ARMING_FUTURE_DATED` |
| 5 | `sessions_used > max_sessions` | `lapsed` | `LIVE_ARMING_LAPSED` |
| — | otherwise | `armed` | *(none)* |

The ceremony's own refusals, each raised as a `LiveArmingRefused` the CLI
prints and exits `2` on: `LIVE_ENVELOPE_MISSING`,
`LIVE_ARMING_INSTANCE_UNSEALED`,
`LIVE_ARMING_TTL_INVALID`, `LIVE_ARMING_TOKEN_INVALID`,
`LIVE_ARMING_PLAN_EXPIRED`, `LIVE_ARMING_INPUTS_CHANGED`,
`LIVE_ARMING_NOT_ARMED`.

`ARMING_REASON_CODES` in `live_arming.py` is the frozen set of every arming
reason code and the source of truth for the corpus: the five status codes in
the table above; the seven the ceremony raises directly; the three
ENTER-admission codes the live authority added in slice 7
(`LIVE_ARMING_REQUIRED`, `LIVE_ARMING_UNOBSERVED`, `LIVE_ARMING_LEDGER_INVALID`
— see [alpaca-live-authority](alpaca-live-authority.md)); the halt's
`LIVE_VERDICT_TRANSITION_HALT`, which is the sync's once-per-transition log
action and never reaches a receipt (the receipt carries the instance's own
arming code); and `LIVE_SHADOW_INCOMPLETE`, which stays
defined for the verdict's vocabulary though no code path raises it since
slice 7 (shadow is a mode, not a requirement — owner decision 2026-09-09).
This note does not repeat the count; the set does.

## Lapse, and a worked example

```
sessions_used = trading_session_count(ET date of armed_at_ms, ET date of now_ms)
lapsed        = sessions_used > max_sessions
```

Both ET dates are inclusive. `trading_session_count` is the canonical calendar
(`app/lean_sidecar/trading_calendar.py`) and `et_date_at_ms` is the canonical ET
anchor (`app/utils/session_anchors.py`); nothing on this path computes either
itself, so there is no session literal anywhere on it. The arming session counts as one — an
arming at 15:59 ET spends a whole session on a minute, which is why `status`
reports `sessions_remaining` rather than a wall-clock expiry.

Worked, with `max_sessions` = 2 for the example, armed **Friday 2026-09-11 at
10:00 ET**:

| ET date | A session? | `sessions_used` | `sessions_remaining` | State |
|---|---|---|---|---|
| Fri 2026-09-11 | yes | 1 | 1 | `armed` |
| Sat 2026-09-12 | no | 1 | 1 | `armed` |
| Sun 2026-09-13 | no | 1 | 1 | `armed` |
| Mon 2026-09-14 | yes | 2 | 0 | `armed` — the last session it bought |
| Tue 2026-09-15 | yes | 3 | 0 | `lapsed`, `LIVE_ARMING_LAPSED` |

The weekend spends nothing. So does a market holiday: armed **Wednesday
2026-11-25** with the same grant, Thanksgiving (Thu 2026-11-26) is not a
session, the half day (Fri 2026-11-27) is, and the arming lapses on Mon
2026-11-30 — its third session.

An arming performed on a non-trading ET date simply starts counting at the next
session, which is what the inclusive count over the calendar already says.

## How the envelope gets sealed

Slice 5 shipped `envelope_agreement` with `sealed` permanently `None`, so it
could only ever answer `unsealed` and `LIVE_ENVELOPE_DISAGREEMENT` was
unreachable. Slice 6 supplies the missing half: on every 15 s tick
`LiveEnvelopeSync` re-reads the account's arming ledger and assigns
`LiveEnvelopeGate.sealed` from the **newest arming record's** own
`envelope_values` (a revocation never unseals an account — it withdraws one
instance's permission). Each transition is logged once, `live_envelope_sealed`
or `live_envelope_unsealed`, with the resulting agreement.

The effect: once any instance on the account has been armed, editing an
`ALPACA_LIVE_*` value makes every rehearsal ENTER refuse
`LIVE_ENVELOPE_DISAGREEMENT` through the existing `require_envelope_admission`,
until a re-arm seals the new numbers. Changing a bound is a re-arm, never a
silent drift.

A ledger nobody can read seals nothing: `LiveArmingInvalid` returns the gate to
unsealed and is logged at **error** level, once per transition
(`live_arming_ledger_invalid`), and the verdict counts zero armed instances and
names the fault in its detail.

Per-instance arming **is** consulted at ENTER admission on the live authority
(slice 7); under the shadow authority the sealed envelope stays account-level
and rehearsal ENTERs are not gated on arming.

## Operator recipe

Every command writes exactly one JSON object to stdout. Exit `0` answered,
`1` the command cannot be run as asked, `2` the ceremony refused.

The Clerk authority this ceremony reads and writes lives on the VM-local
`alpaca-clerk-data` named volume the running worker mounts at
`/app/artifacts/alpaca_clerk` (see `compose.yaml`), not on the host tree at
`PythonDataService/artifacts/alpaca_clerk` — that host tree is mounted
read-only at `/app/alpaca_clerk_legacy` and normal runtime never reads
authority from it. Run every command from the repo root (where
`compose.yaml` lives) inside a one-shot `python-service` container against
that same volume, the same pattern the
[SQLite Clerk recovery/cutover runbook](../runbooks/alpaca-sqlite-clerk-recovery-and-cutover.md)
uses. A host-side `python -m scripts.manage_alpaca_arming` invocation writes
into the unmounted legacy tree instead: the worker never sees it, `status`
keeps reporting `unarmed`, and every live ENTER keeps refusing
`LIVE_ARMING_REQUIRED` no matter what `apply` just wrote.

```bash
# What is armed on the shadowed live account, and how much of each grant is left.
podman compose run --rm --no-deps python-service \
  python -m scripts.manage_alpaca_arming \
  --artifacts-root /app/artifacts/alpaca_clerk \
  --live-state-root /app/artifacts/live_runs \
  status

# Propose an arming. Read-only: it writes nothing but the optional plan file.
# --plan-out must land under the host-bind-mounted /app/artifacts (not /tmp,
# which is private to the --rm container and gone once it exits) so the
# apply step below, run in its own container, can read the plan back.
podman compose run --rm --no-deps python-service \
  python -m scripts.manage_alpaca_arming \
  --artifacts-root /app/artifacts/alpaca_clerk \
  --live-state-root /app/artifacts/live_runs \
  plan --strategy-instance-id ema-shadow-1 --plan-out /app/artifacts/arming-plan.json

# Confirm it, within 120 s, quoting the token the plan printed.
podman compose run --rm --no-deps python-service \
  python -m scripts.manage_alpaca_arming \
  --artifacts-root /app/artifacts/alpaca_clerk \
  --live-state-root /app/artifacts/live_runs \
  apply --plan-file /app/artifacts/arming-plan.json --confirmation-token <confirmation_token>

# Revoke before the lapse. One append, no confirmation: the closed direction.
podman compose run --rm --no-deps python-service \
  python -m scripts.manage_alpaca_arming \
  --artifacts-root /app/artifacts/alpaca_clerk \
  disarm --strategy-instance-id ema-shadow-1
```

`--artifacts-root` and `--now-ms` exist for tests and for an operator pointing
at a non-default tree; the invocations above pass `--artifacts-root`
explicitly so the command is correct regardless of what the container
image's own default resolves to. `--live-state-root` (accepted by `status`,
`plan` and `apply`, not by `disarm` — the closed direction reads no binding)
defaults to the runner's own `live_artifacts_root()` — where sealed bot
bindings live, a different tree from the Clerk artifacts root the rest of
arming's evidence sits under — and is passed explicitly above for the same
reason. There is no force mode and no HTTP route: an arming is a supervised,
out-of-process act.

## In the live verdict

| Field | What slice 6 makes it say |
|---|---|
| `armed_instance_count` | how many instances bound to this live account are `armed` right now |
| `envelope_state` | `sealed` once the ledger holds at least one arming record, else `configured_unsealed` |
| `final_verdict` | `live-armed` when the account is live, mode agreed, a Clerk installed and the count is at least 1; otherwise `live-unarmed` |

The headline names the count; the detail states whether real-money submission
is open on this authority and names every instance the ledger knows that is
not armed, with its reason code. The schema is unchanged — every one of these
values was already declared in slice 1.

## Residuals

- **Resolved by `arming_admission.py`.** An arming record admits a real-money
  ENTER now — the third admission in the chain, on the live authority.
- **Resolved by the sync's `LIVE_MODE_DISAGREEMENT` path.** Mid-session
  broker-mode disagreement is caught there, not by the ceremony re-contacting
  the broker.
- **Resolved by `require_arming_admission`.** Per-instance disagreement is
  enforced per instance at ENTER admission on the live authority, not merely
  reported by `status` and the verdict.
- **One shadowed account per artifacts root — for arming, not for disarm.**
  `live_account_id_for` refuses rather than choosing when the activation fence
  names two, because an arming has no basis to pick one. `disarm` does **not**
  inherit that limit, and no longer needs the activation proof at all: the
  closed direction must survive the loss of the evidence that opened it, since
  a deleted or damaged fence is exactly the incident a revocation exists for.
  When the fence cannot name one account, `LiveArmingLedger.discover` scans
  `accounts/arming/*/live_arming.jsonl` for the ledger whose rows name the
  instance — an arming row already carries its own live account — and the
  revocation is appended there. A directory that is not a real account id, and
  a ledger that will not verify, are skipped and logged at **error** level
  (`live_arming_ledger_invalid`, with the traceback), never silently; no ledger
  naming the instance is `LIVE_ARMING_NOT_ARMED`; two accounts having armed the
  same instance id is `LIVE_ARMING_INSTANCE_UNSEALED` naming both, because
  nothing in the tree can choose between them either.
- **`ALPACA_LIVE_ARMING_MAX_SESSIONS` has no upper bound in code** — the owner
  rejected numbers in code. The plan output shows exactly how many sessions the
  arming buys, so an implausible grant is visible at the moment it is confirmed.
- **Resolved: `observe_arming` reads on every live-custodying facade authority
  now** (shadow, or sqlite on `real_live`), not the shadow authority alone. On
  the `real_live` world it counts only live-sealed instances: the rehearsal's
  `shadow:`-sealed bindings are foreign to a graduated authority (slice 7 R15).
- **Resolved by `ArmingGate.invalidate`.** An unreadable ledger is a refusal
  at ENTER admission now, not an absent arming.
- **A record dated after the clock disarms; it never extends.** `now_ms`
  behind a record's `armed_at_ms` reports `disarmed` / `LIVE_ARMING_FUTURE_DATED`
  rather than deferring the lapse count: a rolled-back clock must never buy an
  arming more sessions than it was granted (controller ruling, ADR 0059 D3
  addendum).

## Decision record

[ADR 0059](../architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md)
Decision 3 (arming), Decision 4's sealing sentence (the envelope is read and
sealed at arming; a difference between the environment and the sealed record is
`LIVE_ENVELOPE_DISAGREEMENT`) and Decision 8 (the verdict's
`armed_instance_count`, `envelope_state`, `final_verdict`). The controller
rulings R1–R14 that fill in what the ADR left open are recorded in
`docs/superpowers/specs/2026-09-09-live-slice-6-arming-ceremony-design.md`.
Predecessors: [alpaca-shadow-authority](alpaca-shadow-authority.md) (the
receipt this ceremony reads) and [alpaca-live-envelope](alpaca-live-envelope.md)
(the values it seals).
