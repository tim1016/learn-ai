# Add an Alpaca account to learn-ai

**Status:** Sanctioned path. Owner decision, 2026-09-15: there is no in-product
"activate this account" button and none is planned — the operator runs this
ceremony by hand, and the product's job is only to say clearly, in
`ACTIVATION_REQUIRED`, that it is needed (see [§5](#5-activation_required-is-normal-not-a-fault)).
This is that ceremony, start to finish, for both Paper and Live. Where the
two modes differ, the difference is called out inline at the step it affects
— this is deliberately one document, not a Paper half and a Live half that
can drift apart.

**Authority:** [ADR 0062](../architecture/adrs/0062-broker-clerk-fleet-control-plane.md)
(fleet control plane), [ADR 0059](../architecture/adrs/0059-real-money-live-behind-shadow-gate-arming-and-cash-bound-envelope.md)
(Live graduation and arming), [ADR 0060](../architecture/adrs/0060-broker-configuration-is-a-user-owned-profile-on-the-clerk-volume.md)
(broker configuration profiles). This runbook is the parent procedure for
three existing, narrower documents and does not restate their commands:
[the fleet dev two-lane posture](fleet-dev-two-lane-posture.md) (lane
provisioning and Compose topology), [the Alpaca SQLite Clerk recovery and
cutover subprocedure](alpaca-sqlite-clerk-recovery-and-cutover.md) (the
activation ceremony itself), and [the disposable Paper clean-slate
runbook](alpaca-clerk-disposable-paper-clean-slate.md) (resetting a Paper
account to try again). Read all three once before running anything below.

## Scope

"Adding an account" means: an Alpaca Paper or Live account becomes bound to
a Clerk lane in this deployment, its custody authority activates, and it
reaches `lifecycle: ready` in the fleet directory with real broker custody
behind it — not just a saved credential. This runbook assumes the **two-lane
fleet posture** described in `fleet-dev-two-lane-posture.md` (one dedicated
container per lane — `alpaca-paper-clerk`, `alpaca-live-clerk`), because
that is what runs on this dev machine and what this runbook is proven
against. If your deployment still runs the single combined `python-service`
role instead, substitute `python-service` for the lane container name in
every `podman exec` / `podman restart` below, and read
`fleet-dev-two-lane-posture.md#why-the-combined-role-cannot-host-a-second-lane`
first — a combined role can host at most one account, ever.

## 0. Prerequisites

- The stack is up (`./restart.sh` from the repo root; see `.claude/CLAUDE.md`).
- A host Python venv at `PythonDataService/.venv`, provisioned with
  `./bootstrap-host-venv.sh` from the repo root. Commands below marked
  "runs on: host" that invoke `.venv/bin/python` use this venv.
- You have decided **Paper** or **Live** for this account, and you know
  which lane (which Clerk container) it is going into. A lane serves
  exactly one account for its lifetime — [see the "why" in
  `fleet-dev-two-lane-posture.md`](fleet-dev-two-lane-posture.md#why-the-combined-role-cannot-host-a-second-lane).
  If no free lane exists yet, [§2](#2-provision-the-lane-skip-if-reusing-an-existing-lane)
  provisions one.

## 1. Get Alpaca API credentials

**Paper:** log in to the [Alpaca dashboard](https://app.alpaca.markets),
switch to Paper Trading, and generate an API key/secret pair. This is
instant — no approval, no funding.

**Live:** Live API keys are issued only once Alpaca has approved and funded
the brokerage account. That approval is an out-of-band process with Alpaca,
not something this repo can do for you — do not start the steps below until
you already have the Live key/secret pair in hand.

Whichever mode, you now have a `key_id` / `secret_key` pair and know the
account's Alpaca account number (Paper numbers are prefixed `PA...`; Live
numbers are plain numeric, e.g. `318420190`). Treat the secret key like any
other credential: it goes straight into a gitignored env file (§3), never
into a ticket, a chat message, or shell history.

## 2. Provision the lane (skip if reusing an existing lane)

If the account is going into a lane that doesn't exist yet, provision it —
create the named volume, register the Clerk identity with the coordinator,
and approve its endpoint — following
[`fleet-dev-two-lane-posture.md` § Ceremonies performed](fleet-dev-two-lane-posture.md#ceremonies-performed-host-side-one-shot-containers).
That section is the authority for the exact `manage_broker_fleet` commands;
do not improvise a variant here. Confirm the new container appears in
`podman compose ps` before continuing.

If you're reusing an already-provisioned, still-unbound lane (for example,
a lane whose account was cleared by the reset in [§8](#8-starting-over-on-a-disposable-paper-account-only)),
skip straight to §3.

## 3. Put the credentials on the lane

Each lane reads its Alpaca credentials from its own gitignored env file —
`deploy/fleet/env/paper.env` (slot `default`) for Paper,
`deploy/fleet/env/live.env` (slot `live`) for Live — never from
`PythonDataService/.env` in the two-lane posture (that file must hold no
Alpaca execution credential; see
[`fleet-dev-two-lane-posture.md` § Secrets layout](fleet-dev-two-lane-posture.md#secrets-layout-all-gitignored)).
Set the `key_id` / `secret_key` pair from §1 in the matching file,
`chmod 600` it, then recreate the lane so it picks up the new environment:

```bash
# runs on: host, repo root
chmod 600 deploy/fleet/env/paper.env deploy/fleet/env/live.env
podman compose up -d alpaca-paper-clerk   # or alpaca-live-clerk
```

Then confirm the lane's HTTP surface is answering, checked from the
coordinator's own network (the lane has no host-published port) — the exact
probe is documented as step 1 of
[`fleet-dev-two-lane-posture.md` § Fresh-lane
bootstrap](fleet-dev-two-lane-posture.md#fresh-lane-bootstrap-first-binding).
The endpoint has already been approved in §2 (or in a prior provisioning
run); this only confirms the container itself is answering before you try
to bind it.

## 4. Bind the account through the desk

Every online lane heartbeats and reports `binding_pending` from the moment
it starts, with no account bound yet — this is expected, not an error (see
[`fleet-dev-two-lane-posture.md` § Fresh-lane bootstrap](fleet-dev-two-lane-posture.md#fresh-lane-bootstrap-first-binding)
for the mechanics of why there's no race). Open the desk at
`http://localhost:4200/brokers/alpaca`, select the lane (or go straight to
`/brokers/alpaca/clerks/<clerk_id>/configuration`), and create a
configuration profile choosing the credential slot (`default` for Paper,
`live` for Live) and matching endpoint mode. Verify the account, pin it,
stage the selection, and Apply. Restart the lane once more so the applied
selection binds:

```bash
# runs on: host
podman restart alpaca-paper-clerk   # or alpaca-live-clerk
```

Follow `fleet-dev-two-lane-posture.md § Fresh-lane bootstrap` for the exact
API shape if you're driving this by hand instead of through the desk (it
needs a `command_context` envelope with `capability: "configuration_manage"`
on every mutating call). After this restart the binding confirms and the
lane's heartbeat reports `binding_confirmed` — but custody is not yet open.
That's §5 and §6.

## 5. `ACTIVATION_REQUIRED` is normal, not a fault

Once the account is bound, a Paper lane that has never been activated
starts reporting `ACTIVATION_REQUIRED`
(`app/broker/alpaca/clerk/active_authority.py:245`, as of this writing).
Read this as "the
supervised cutover in §6 has not run yet," not as something broken — it is
the expected, correct state of every brand-new Paper account before its
ceremony, and it will not clear itself. There is no retry, no timeout, no
flag to skip it.

**The asymmetry, stated plainly, so it doesn't surprise you:** a *Live*
account without an activation record does **not** hard-fail this way — it
falls back to the shadow authority (reads the real account, submits
nothing) and still reaches `lifecycle: ready`. A *Paper* account without an
activation record refuses to start custody at all. Paper is, today,
strictly harder to bootstrap than real money: Live can sit in shadow
indefinitely and look "done" from the directory, while Paper cannot compose
any authority — real or shadow — until §6 completes. If you're standing up
a Live account and only want shadow rehearsal for now, you can stop after
§4. If you're standing up Paper, or you want Live to actually hold
real-money custody, §6 is not optional.

## 6. Activate custody: the cutover ceremony

This is the [SQLite Clerk recovery and cutover
subprocedure](alpaca-sqlite-clerk-recovery-and-cutover.md), specifically its
[§ Human cutover: initialize, plan, then
apply](alpaca-sqlite-clerk-recovery-and-cutover.md#human-cutover-initialize-plan-then-apply).
That document owns the exact CLI flags, the retry semantics, and the
refusal conditions — this section only supplies what it assumes you already
have (fresh broker evidence) and the two things about it that are not
obvious the first time.

For the two-lane posture, run every `manage_alpaca_sqlite_clerk` command
below **inside the lane's own long-running container** — not in a separate
one-shot `python-service` container, which is the pattern for the default
combined layout instead:

```bash
# runs on: inside alpaca-paper-clerk (or alpaca-live-clerk)
podman exec -it alpaca-paper-clerk /opt/venv/bin/python -m scripts.manage_alpaca_sqlite_clerk \
  --artifacts-root /app/artifacts/alpaca_clerk \
  --account-id <ACCOUNT_ID> \
  <operation> [flags...]
```

A brand-new account has no bots and nothing else holding its (not yet
existing) database, so the general "stop every governed bot first" boundary
in the subprocedure is trivially satisfied here — there is nothing running
against this account yet.

### Capture broker evidence

Every step in this ceremony — `cutover-initialize`, the verified backup in
between, `cutover-plan`, and `cutover-apply` — consumes a JSON evidence file
proving the account is flat and order-free at the moment it was captured.
The tool never calls Alpaca itself (it is deliberately broker-free); you
capture the evidence yourself, straight from Alpaca's REST API, and hand it
the file:

```bash
# runs on: host (or wherever the lane's credentials are reachable)
KEY_ID=$(grep ^ALPACA_API_KEY_ID= deploy/fleet/env/paper.env | cut -d= -f2-)
SECRET_KEY=$(grep ^ALPACA_API_SECRET_KEY= deploy/fleet/env/paper.env | cut -d= -f2-)
curl -s https://paper-api.alpaca.markets/v2/account \
  -H "APCA-API-KEY-ID: $KEY_ID" -H "APCA-API-SECRET-KEY: $SECRET_KEY" \
  > /tmp/alpaca-account.json
curl -s "https://paper-api.alpaca.markets/v2/orders?status=open" \
  -H "APCA-API-KEY-ID: $KEY_ID" -H "APCA-API-SECRET-KEY: $SECRET_KEY" \
  > /tmp/alpaca-open-orders.json
```

Use `https://api.alpaca.markets` for Live. Keep both raw responses on the
volume as the ceremony's retained proof (e.g.
`accounts/alpaca/<account_id>/broker_captures/cutover-<date>/`), and
hand-assemble the evidence file the CLI actually reads — the shape is fixed
by the subprocedure's [§ Broker evidence
files](alpaca-sqlite-clerk-recovery-and-cutover.md#broker-evidence-files):

```json
{
  "account_id": "<ACCOUNT_ID>",
  "account_mode": "paper",
  "observed_at_ms": 1800000000000,
  "proof_reference": "accounts/alpaca/<ACCOUNT_ID>/broker_captures/cutover-2026-09-15/",
  "positions": {},
  "open_order_ids": []
}
```

`observed_at_ms` is `int64 ms UTC` (per this repo's temporal-rigor rule) —
the wall-clock instant you captured the two API responses above, not a
rounded or reformatted value. `account_mode` is the account's real mode
(`"paper"` or `"live"`), and it must agree with what the lane itself is
configured for. A genuinely fresh account has no positions and no open
orders, so `positions: {}` and `open_order_ids: []` are correct as written
— do not invent a placeholder symbol entry.

### Two things that were proven the hard way on 2026-09-15

Both of these were hit for real during this ceremony's first live run, and
both will bite you again if you skip this paragraph.

1. **A WAL/SHM sidecar refuses planning — and the fix is a checkpoint,
   never file deletion.** A lane whose Clerk container stays up between
   attempts can accumulate a stale, 0-byte `clerk.db-wal` + `clerk.db-shm`
   pair, left behind by a connection that exited without a clean close.
   `cutover-plan` refuses to run against it (the refusal text itself says
   "remove no files manually" — believe it). With nothing holding the
   database open, connect once and run `PRAGMA wal_checkpoint(TRUNCATE)`,
   then close cleanly; SQLite removes the sidecar files itself. Never `rm`
   a `-wal` or `-shm` file by hand.
2. **`cutover-apply` refuses evidence that differs from the plan's, byte
   for byte.** Broker evidence is captured once (the curl calls above);
   `cutover-plan` and `cutover-apply` must both run against that *same*
   evidence file, and both inside that one capture's
   `--max-evidence-age-ms` window. Capturing fresh evidence between `plan`
   and `apply` — even if the account state hasn't actually changed — will
   make `apply` refuse with a normalized-evidence mismatch. Capture once,
   run `initialize` → backup → `plan` → `apply` in one sitting, and don't
   re-curl Alpaca in between.

### Run the ceremony

Follow the subprocedure's [§ Establish the inactive
generation](alpaca-sqlite-clerk-recovery-and-cutover.md#establish-the-inactive-generation)
(`cutover-initialize`), then its own instruction to publish a [verified
online backup](alpaca-sqlite-clerk-recovery-and-cutover.md#verified-online-backup),
then [§ Produce the read-only
plan](alpaca-sqlite-clerk-recovery-and-cutover.md#produce-the-read-only-plan)
(`cutover-plan`) and finally `cutover-apply` with the token that command
prints — using the container form from the top of this section and the one
evidence file from above throughout. Review every receipt as it's
produced; none of these steps are silent.

## 7. Verify the account is genuinely live

Three independent checks, all from the host:

**Authority generation moved 0 → 1.** The fleet directory reports
`effective_binding_generation` per clerk:

```bash
# runs on: host, repo root
curl -s localhost:8000/api/broker-clerks \
  -H "X-Data-Plane-Control-Intent: learn-ai-browser-control" \
  -H "X-Data-Plane-Control-Secret: $(grep ^DATA_PLANE_CONTROL_SECRET= .env | cut -d= -f2-)" \
  | jq '.clerks[] | {clerk_id, lifecycle_state, effective_binding_generation}'
```

`effective_binding_generation` for this lane's `clerk_id` must read `1`
(not `null`, not `0`) after §6's `cutover-apply` succeeds and the lane
restarts.

**The lane reports `ready`.** Same output, same command:
`lifecycle_state` must read `"ready"`, not `"starting"`, `"degraded"`, or
`"unreachable"`. Its `provider_summary` names the account mode and the
composed authority kind — `real_paper` for an activated Paper account,
`real_live` for an activated Live account, or `shadow` if you stopped after
§4 for a Live account per the asymmetry in §5. Seeing `shadow` here for a
Paper account is itself a bug report — Paper has no shadow fallback.

**The desk shows it.** Open `http://localhost:4200/brokers/alpaca` — the
new lane appears in the directory as `ready`, and its account page (via the
lane's own clerk link) shows a real account snapshot: balances, buying
power, and (for a genuinely fresh account) zero positions and zero open
orders, pulled live from Alpaca rather than placeholder data.

If any of the three disagrees with the others — directory says `ready` but
the desk hasn't updated, or the generation moved but the summary still says
`shadow` — do not paper over it by re-running the ceremony. Stop and
compare against the receipts §6 produced; this is exactly the kind of
divergence `docs/references/reconciliations/` and the `reconcile-backtest`
skill exist for.

**Activation is not arming.** For Live, reaching `real_live` here makes the
account's *custody* authority real — it does not by itself let any bot
submit a real order. Each strategy instance additionally needs its own
arming ceremony before its first real-money `ENTER`; see
[`docs/references/alpaca-live-arming.md`](../references/alpaca-live-arming.md).
Paper has no separate arming step.

## 8. Starting over on a disposable Paper account only

If this was a throwaway Paper account and you want to redo this whole
runbook against a clean slate instead of standing up a new lane, use the
[disposable Paper clean-slate
runbook](alpaca-clerk-disposable-paper-clean-slate.md) — it quarantines the
account's SQLite authority and bot directories and clears its saved
configuration, then you re-enter at §4 (bind) once it completes. It
explicitly refuses Live and Shadow targets; there is no equivalent shortcut
for Live. Do not attempt to "reset" a Live account by any path other than
the supervised `reset` operation in the cutover subprocedure, and only with
a separately reviewed reason.

## Related

- [Alpaca SQLite Clerk recovery and cutover subprocedure](alpaca-sqlite-clerk-recovery-and-cutover.md)
- [Dev-stack two-lane fleet posture](fleet-dev-two-lane-posture.md)
- [Alpaca Clerk disposable Paper clean slate](alpaca-clerk-disposable-paper-clean-slate.md)
- [Alpaca live arming](../references/alpaca-live-arming.md)
- [Broker clerk fleet authority](../broker-clerk-fleet-authority.md)
