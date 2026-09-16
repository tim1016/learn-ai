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
- `jq`, on the host — every JSON response this runbook parses (§6's broker
  captures, §7's fleet-directory check) is parsed with it, not by eye.
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
`deploy/fleet/env/paper.env` (slot `default`, variables `ALPACA_API_KEY_ID`
/ `ALPACA_API_SECRET_KEY`) for Paper, `deploy/fleet/env/live.env` (slot
`live`, variables `ALPACA_CREDENTIAL_LIVE_KEY_ID` /
`ALPACA_CREDENTIAL_LIVE_SECRET_KEY`) for Live — never from
`PythonDataService/.env` in the two-lane posture (that file must hold no
Alpaca execution credential; see
[`fleet-dev-two-lane-posture.md` § Secrets layout](fleet-dev-two-lane-posture.md#secrets-layout-all-gitignored)).
The two committed `.env.example` templates are **not interchangeable**:
copy the one matching this account's mode and confirm, by variable name
only, that it still names the pair above for that mode — never print a
value. Live's template carried the wrong (default-slot) names until this
fix; see #2156 and the slot mapping in
`app/broker/alpaca/profile/credentials.py:59-66`.

Set the `key_id` / `secret_key` pair from §1 in the matching file, then
`chmod 600` it and recreate **that lane only** so it picks up the new
environment. A bare `podman restart` does not re-read an env file — the
container keeps whatever environment it was created with — so this has to
be a Compose recreate, and it has to resolve the same file set
`restart.sh` does: `compose.yaml` alone declares neither clerk service, and
an `-f` list that stops at `compose.fleet.dev.yaml` recreates the lane
**without** `compose.override.yaml`, silently dropping its per-clerk
identity:

```bash
# runs on: host, repo root
chmod 600 deploy/fleet/env/paper.env   # or deploy/fleet/env/live.env — only this account's file
podman compose -f compose.yaml -f compose.fleet.dev.yaml -f compose.override.yaml \
  up -d --force-recreate alpaca-paper-clerk   # or alpaca-live-clerk
```

Then confirm the lane's HTTP surface is answering, checked from the
coordinator's own network (the lane has no host-published port) — the exact
probe is documented as step 1 of
[`fleet-dev-two-lane-posture.md` § Fresh-lane
bootstrap](fleet-dev-two-lane-posture.md#fresh-lane-bootstrap-first-binding).
The endpoint has already been approved in §2 (or in a prior provisioning
run); this only confirms the container itself is answering, not that it
holds the credentials you just wrote — every field of
`AlpacaCredentialEnvironment` is optional and the class "never refuses to
construct" (`app/broker/alpaca/profile/credentials.py:74-90`), so a lane
recreated with empty, stale, or a **different account's** credentials
answers `/health` exactly like a correctly recreated one. Worst case on
§2's "reusing an already-provisioned lane" path: the recreate silently
doesn't take, §4 binds and pins whichever account the container actually
holds, and the operator believes they onboarded the new one — wrong lane,
wrong account, no error. Close that gap by comparing a hash, never a
credential value:

```bash
# runs on: host — the container's credential must hash-match the file you
# just wrote; a mismatch means the recreate above didn't actually take
HOST_HASH=$(grep ^ALPACA_API_KEY_ID= deploy/fleet/env/paper.env | cut -d= -f2- | shasum -a 256 | cut -d' ' -f1)
CONTAINER_HASH=$(podman exec alpaca-paper-clerk /opt/venv/bin/python -c \
  "import hashlib, os; print(hashlib.sha256(os.environ.get('ALPACA_API_KEY_ID', '').encode()).hexdigest())")
[ "$HOST_HASH" = "$CONTAINER_HASH" ] \
  || { echo "REFUSE: container credentials do not match deploy/fleet/env/paper.env" >&2; exit 1; }
```

(For Live: `ALPACA_CREDENTIAL_LIVE_KEY_ID`, `deploy/fleet/env/live.env`,
`alpaca-live-clerk`.)

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

### Create the runner-artifacts root's `live_state/` directory first

Before anything else: `cutover-initialize` calls
`read_quiescent_alpaca_roster` (`app/engine/live/cutover_roster.py:65`),
which hard-refuses — as a `CutoverRefused`, propagated straight through
`cutover.py`'s `_runner_roster_evidence` — if
`<runner-artifacts-root>/live_state` does not exist as a real directory.
That refusal is **not** the same thing as the "no bots governed" case this
runbook otherwise leans on (§6's opening paragraph): the owner-decided
forgiveness in `_runner_roster_evidence` only covers an *existing*
`live_state/` that happens to be empty of Alpaca bots, never a missing
root. A genuinely fresh lane's volume has never had a bot on it, so it has
no `live_state/` directory at all yet — you will hit this refusal on your
very first `cutover-initialize` unless you create the directory first. This
is exactly what `fleet-dev-two-lane-posture.md` § Paper activation
boundary's step 1 recorded from the real 2026-09-15 run.

In the two-lane posture the lane container has no separate runner-artifacts
volume — its own writable-root fence keeps every piece of lane state inside
the one custody volume it does mount (`IBKR_LIVE_RUNS_ROOT`,
`IBKR_LIVE_BARS_ROOT`, and `BROKER_CAPTURE_DIR` in its compose environment
all resolve under `/app/artifacts/alpaca_clerk`, the same path
`--artifacts-root` already points at). Pass that same path as
`--runner-artifacts-root` below, and create the directory there before the
first command:

```bash
# runs on: inside alpaca-paper-clerk (or alpaca-live-clerk)
podman exec alpaca-paper-clerk mkdir -p /app/artifacts/alpaca_clerk/live_state
```

If you're instead reusing an established runner-artifacts tree that already
has bots on it (the default combined layout, or a lane that mounts a
separate runner volume), this directory already exists and the step above
is a no-op — the check only refuses a *missing* root, never an existing
one.

### Capture broker evidence — twice, not once

This ceremony needs broker evidence at two separate moments, and they are
*meant* to be two different captures: once for `cutover-initialize`, and
again — a genuinely fresh capture — after the verified backup, immediately
before `cutover-plan`. The subprocedure says this explicitly (["Capture
fresh broker evidence again after the
backup"](alpaca-sqlite-clerk-recovery-and-cutover.md#produce-the-read-only-plan)).
Don't read the rest of this runbook as "one capture for the whole
ceremony" — it's one capture for `initialize`, then a second capture that
`plan` and `apply` share (see gotcha 2 below for why that second pair must
be exact).

The tool never calls Alpaca itself (it is deliberately broker-free); you
capture the evidence yourself, straight from Alpaca's REST API, using
whichever credential pair matches the account's real mode — the same slot
split as §3, not the same variable names for both:

```bash
# runs on: host — Paper (slot `default`, deploy/fleet/env/paper.env)
KEY_ID=$(grep ^ALPACA_API_KEY_ID= deploy/fleet/env/paper.env | cut -d= -f2-)
SECRET_KEY=$(grep ^ALPACA_API_SECRET_KEY= deploy/fleet/env/paper.env | cut -d= -f2-)
BASE_URL=https://paper-api.alpaca.markets
```

```bash
# runs on: host — Live (slot `live`, deploy/fleet/env/live.env)
KEY_ID=$(grep ^ALPACA_CREDENTIAL_LIVE_KEY_ID= deploy/fleet/env/live.env | cut -d= -f2-)
SECRET_KEY=$(grep ^ALPACA_CREDENTIAL_LIVE_SECRET_KEY= deploy/fleet/env/live.env | cut -d= -f2-)
BASE_URL=https://api.alpaca.markets
```

Live's key/secret variables are a genuinely different pair, not the same
names in a different file — greping `ALPACA_API_KEY_ID` out of `live.env`
finds nothing there and silently hands `curl` an empty credential. Then,
with whichever pair applies:

```bash
# runs on: host
curl -sS --fail-with-body "$BASE_URL/v2/account" \
  -H "APCA-API-KEY-ID: $KEY_ID" -H "APCA-API-SECRET-KEY: $SECRET_KEY" \
  > /tmp/alpaca-account.json
curl -sS --fail-with-body "$BASE_URL/v2/orders?status=open" \
  -H "APCA-API-KEY-ID: $KEY_ID" -H "APCA-API-SECRET-KEY: $SECRET_KEY" \
  > /tmp/alpaca-open-orders.json
```

`--fail-with-body` is load-bearing, not decoration. Bare `curl -s` exits `0`
on a 401/403 and writes the error body into the capture file as if it were
account data — the cutover tool never talks to Alpaca itself (it is
deliberately broker-free), so a bad credential pair would otherwise sail
straight through these files, past the JSON you hand-assemble below, and
into a completed cutover with nothing having verified real broker state. If
either `curl` above exits non-zero, stop: the file it wrote is an error
response, not evidence.

**Read what you captured before typing anything below — this is the step
that actually establishes flatness, not an assumption about "genuinely
fresh" accounts:**

```bash
# runs on: host
jq -e '((.long_market_value // "0") | tonumber) == 0
       and ((.short_market_value // "0") | tonumber) == 0' \
  /tmp/alpaca-account.json \
  || { echo "DISQUALIFIED: account is not flat — stop, do not cut over" >&2; exit 1; }
jq -e 'length == 0' /tmp/alpaca-open-orders.json \
  || { echo "DISQUALIFIED: account has open orders — stop, do not cut over" >&2; exit 1; }
```

Both checks must pass before you write `positions: {}` and
`open_order_ids: []` below — that JSON is a transcription of what the two
files above actually say, not a default you're entitled to assume. If
either check fails, this account is not a candidate for the fresh-account
path this runbook documents; stop and escalate rather than hand-editing a
non-empty result away. For Live this is the only thing standing between a
non-flat real-money account and a receipt that claims otherwise —
`_validate_cutover_broker_state` (`app/broker/alpaca/clerk/sqlite/cutover.py:698-716`)
refuses only on what the JSON says, never on anything it fetches itself.

Now retain both raw responses at the exact path `proof_reference` below
will name, inside the lane's own artifacts volume — nothing else copies
them there, and `_validate_cutover_broker_state` checks `proof_reference`
for non-emptiness only (`cutover.py:654-655`, `:706-707`) and never
resolves it as a path, so an unpopulated directory would go unnoticed by
the tool:

```bash
# runs on: host
CAPTURE_DIR=accounts/alpaca/<ACCOUNT_ID>/broker_captures/cutover-2026-09-15
podman exec alpaca-paper-clerk mkdir -p "/app/artifacts/alpaca_clerk/$CAPTURE_DIR"
podman cp /tmp/alpaca-account.json "alpaca-paper-clerk:/app/artifacts/alpaca_clerk/$CAPTURE_DIR/alpaca-account.json"
podman cp /tmp/alpaca-open-orders.json "alpaca-paper-clerk:/app/artifacts/alpaca_clerk/$CAPTURE_DIR/alpaca-open-orders.json"
```

(For Live, target `alpaca-live-clerk`.) Do this for each of the two
captures this ceremony needs (see above) — the second capture's directory
is the one `plan` and `apply` will cite as `proof_reference`. Then
hand-assemble the evidence file the CLI actually reads — the shape is fixed
by the subprocedure's [§ Broker evidence
files](alpaca-sqlite-clerk-recovery-and-cutover.md#broker-evidence-files):

```json
{
  "account_id": "<ACCOUNT_ID>",
  "account_mode": "paper",
  "observed_at_ms": 1800000000000,
  "proof_reference": "accounts/alpaca/<ACCOUNT_ID>/broker_captures/cutover-2026-09-15",
  "positions": {},
  "open_order_ids": []
}
```

`observed_at_ms` is `int64 ms UTC` (per this repo's temporal-rigor rule) —
the wall-clock instant you captured the two API responses above, not a
rounded or reformatted value. `account_mode` is `"paper"` or `"live"`,
matching the account's real mode. `proof_reference` must be the exact
`$CAPTURE_DIR` you just created and populated above, not the illustrative
form this runbook wrote before its own captures existed anywhere.

### Two things that were proven the hard way on 2026-09-15

Both of these were hit for real during this ceremony's first live run, and
both will bite you again if you skip this paragraph.

1. **A WAL/SHM sidecar refuses planning — and the fix is a checkpoint,
   never file deletion.** A lane whose Clerk container stays up between
   attempts can accumulate a stale, 0-byte `clerk.db-wal` + `clerk.db-shm`
   pair, left behind by a connection that exited without a clean close.
   `cutover-plan` refuses to run against it (the refusal text itself says
   "remove no files manually" — believe it). There is no CLI subcommand for
   this (`manage_alpaca_sqlite_clerk` has none; the only callers of the
   checkpoint helper are internal, non-operator-facing scripts), so run it
   directly with the container's own interpreter, against the account's
   database under `--artifacts-root` (`accounts/alpaca/<ACCOUNT_ID>/clerk.db`):

   ```bash
   # runs on: inside alpaca-paper-clerk (or alpaca-live-clerk)
   podman exec alpaca-paper-clerk /opt/venv/bin/python -c \
     "import sqlite3; c = sqlite3.connect('/app/artifacts/alpaca_clerk/accounts/alpaca/<ACCOUNT_ID>/clerk.db'); \
   print(c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()); c.close()"
   ```

   A successful checkpoint prints `(0, ...)` — a nonzero first element means
   something still holds the database open; stop and investigate rather
   than retrying blind. SQLite removes the sidecar files itself once the
   checkpoint succeeds and the connection closes. Never `rm` a `-wal` or
   `-shm` file by hand.
2. **`cutover-apply` refuses evidence that differs from the plan's, byte
   for byte.** `cutover-plan` and `cutover-apply` consume the *same*
   capture — the second one above, taken after the backup, not the earlier
   one used for `initialize` — and both calls must land inside that one
   capture's `--max-evidence-age-ms` window. Capturing a third round of
   evidence between `plan` and `apply` — even if the account state hasn't
   actually changed — will make `apply` refuse with a normalized-evidence
   mismatch, because `observed_at_ms` alone would differ. Run `plan`
   immediately after the second capture, review its output, then run
   `apply` with that identical file before the window closes. The
   subprocedure's own worked examples pass `--max-evidence-age-ms 30000`
   (30 s), which is tight for a human reading a full plan in between; this
   runbook's recommendation is closer to `120000` (2 minutes) so a careful
   review doesn't force a rushed re-capture.

### Run the ceremony

Follow the subprocedure's [§ Establish the inactive
generation](alpaca-sqlite-clerk-recovery-and-cutover.md#establish-the-inactive-generation)
(`cutover-initialize`, with the *first* evidence capture above), then its
own instruction to publish a [verified online
backup](alpaca-sqlite-clerk-recovery-and-cutover.md#verified-online-backup).
Capture broker evidence again — the *second* capture — then follow [§
Produce the read-only
plan](alpaca-sqlite-clerk-recovery-and-cutover.md#produce-the-read-only-plan)
(`cutover-plan`) and finally `cutover-apply` with the token that command
prints, reusing that same second evidence file for both. Use the container
form from the top of this section throughout. Review every receipt as it's
produced; none of these steps are silent.

## 7. Verify the account is genuinely live

Three independent checks, all from the host. Read the first two in light of
the third — they are necessary post-§4 facts, not proof §6 ran:

```bash
# runs on: host, repo root
curl -s localhost:8000/api/broker-clerks \
  -H "X-Data-Plane-Control-Intent: learn-ai-browser-control" \
  -H "X-Data-Plane-Control-Secret: $(grep ^DATA_PLANE_CONTROL_SECRET= .env | cut -d= -f2-)" \
  | jq '.clerks[] | {clerk_id, lifecycle_state, effective_binding_generation,
        endpoint_mode: .provider_summary.endpoint_mode,
        authority_state: .provider_summary.authority_state}'
```

**The load-bearing check: `authority_state`.** `authority_state` (projected
above from `provider_summary.authority_state`, which is where the composed
authority kind actually lives — not a top-level field) is the only one of
the three fields in this payload that actually observes whether §6 ran. It
names the composed authority: `real_paper` for an activated Paper account,
`real_live` for an activated Live account, `shadow` if you stopped after §4
for a Live account per the asymmetry in §5, or **`unavailable`** — the
reading for a lane that completed §4 and has not yet run, or not yet
completed, §6, Paper or Live
(`app/broker/alpaca/clerk/fleet_boot.py:558-563`, the
`.get(authority_kind, "unavailable")` fallback). Seeing `shadow` here for a
Paper account is itself a bug report — Paper has no shadow fallback.
Seeing `unavailable` where you expected `real_paper` / `real_live` means
§6 has not completed; go run it, don't re-run §4.

**Two facts that are true after §4 alone, and prove nothing about §6 by
themselves.** `effective_binding_generation` and `lifecycle_state` both
take their post-bind values as soon as §4's restart lands — a lane that
completed §4 and **skipped §6 entirely** reads `effective_binding_generation: 1`
and `lifecycle_state: "ready"` exactly like a fully activated one, because
neither field consults authority state. `effective_binding_generation` is
the configuration selection's own grant counter
(`get_broker_configuration_service().selection().effective_binding_generation`,
`app/main.py:133`, `:640`), set by §4's `selection/apply` — this is also
why §4 already told you the heartbeat reports `binding_confirmed` at the
end of that section (`binding_is_granted`, `fleet_boot.py:526`, is true at
generation ≥ 1). `_project_lifecycle`
(`app/broker/fleet/service.py:1890-1923`) derives `READY` from session
freshness plus a confirmed binding generation and never looks at authority
state either.

The rule for the generation, stated plainly rather than as the literal `1`
that only holds the first time through: it must have moved up by exactly
one from whatever it was immediately before this cutover. For a first
activation that is `0 → 1`. After a §8 reset, `intended_generation` is
`established.authority_generation + 1`
(`app/broker/alpaca/clerk/sqlite/cutover.py:275-281`), so a lane you're
re-onboarding through this runbook a second time will legitimately read
`2`, `3`, and so on — a generation that merely looks nonzero is not the
check; a generation that moved by exactly one from its pre-cutover value
is, and even that is a post-§4-or-post-reset fact, not proof of §6.
Treat `effective_binding_generation` and `lifecycle_state` as necessary
signals that a bind happened, never as sufficient proof that `cutover-apply`
succeeded — that proof is `authority_state` above, alone.

**The desk shows it.** Open `http://localhost:4200/brokers/alpaca` — the
new lane appears in the directory as `ready`, and its account page (via the
lane's own clerk link) shows a real account snapshot: balances, buying
power, and (for a genuinely fresh account) zero positions and zero open
orders, pulled live from Alpaca rather than placeholder data.

If any of the three disagrees with the others — directory says `ready` but
the desk hasn't updated, or the generation moved but the summary still says
`shadow` — do not paper over it by re-running the ceremony. Stop, preserve
the receipts §6 produced (initialization, backup, plan, and apply), and
treat it as an incident to escalate rather than a transient glitch to
retry. (This is a fleet-lifecycle disagreement, not a backtest trade-log
divergence — the `reconcile-backtest` skill and
`docs/references/reconciliations/` taxonomy are for the latter and don't
apply here.)

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
