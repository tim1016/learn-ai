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
- On the host: `podman` (including `podman cp`, used in §6), and `python3` —
  3.9 or newer, so the macOS system interpreter is enough; the gates below are
  stdlib-only and need no virtualenv. `jq` only for the one JSON projection §7
  asks you to read by eye; no gate in this runbook needs it, or `shasum`
  (GNU/Linux hosts ship `sha256sum` instead, which is why no gate shells out to
  either).
- You have decided **Paper** or **Live** for this account, and you know
  which lane (which Clerk container) it is going into. A lane serves
  exactly one account for its lifetime — [see the "why" in
  `fleet-dev-two-lane-posture.md`](fleet-dev-two-lane-posture.md#why-the-combined-role-cannot-host-a-second-lane).
  If no free lane exists yet, [§2](#2-provision-the-lane-skip-if-reusing-an-existing-lane)
  provisions one.

**Every gate in this runbook is one command whose exit status is the whole
answer.** The gates live in `scripts/alpaca_onboarding_gates.py` — checked,
tested by `scripts/test_alpaca_onboarding_gates.py`, and run from the repo root
— rather than as shell pasted into this page. That is not a style preference:
three earlier drafts of this runbook each shipped a hand-rolled gate that could
not refuse. A credential check that passed when the credential was missing on
*both* sides. A flatness check that passed on a real Alpaca 401 body. A "safe
alternative to `rm`" that created a 0-byte database and called it success. A
gate that needs you to read its output and decide is not a gate. So: if any
command below exits non-zero, stop there — it prints exactly one `REFUSE:` or
`DISQUALIFIED:` line saying why — and if it exits zero, the thing it checked
holds.

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
COMPOSE_ARGS=(--file compose.yaml)
if [[ -f compose.fleet.dev.yaml ]]; then COMPOSE_ARGS+=(--file compose.fleet.dev.yaml); fi
if [[ -f compose.override.yaml ]]; then COMPOSE_ARGS+=(--file compose.override.yaml); fi
podman compose "${COMPOSE_ARGS[@]}" up -d --force-recreate alpaca-paper-clerk   # or alpaca-live-clerk
```

Both optional files are guarded, exactly as `restart.sh:63-68` guards them:
`compose.override.yaml` is gitignored (`.gitignore:12`) and simply absent on a
fresh clone, so a hardcoded third `-f` turns a working host into a failed
recreate.

**Three lists of compose files are in circulation, and this one wins.** The
running paper lane reports `FLEET_COMPOSE_FILES: compose.yaml,compose.override.yaml`;
`compose.fleet.dev.yaml:136` and `:156` commit
`compose.yaml,compose.fleet.dev.yaml` for the two lanes; the block above
resolves whichever of the three files actually exist. Each declared value is
"pasted verbatim into a command an operator runs"
(`app/config.py:112-114`), so the desk can hand you a different list than this
page prints. Take `restart.sh`'s resolution — what the block above reproduces —
as the authority here: it is the only one of the three that describes what is on
*this* host's disk, while a lane's `FLEET_COMPOSE_FILES` is that lane's own
claim about itself. A disagreement is a declaration to fix, not a list to pick
between, and the tiebreak is empirical rather than editorial — `podman compose
"${COMPOSE_ARGS[@]}" config --services` must name the lane you are about to
recreate. If it doesn't, stop: a recreate resolved against the wrong file set is
precisely the silent identity-dropping failure this paragraph exists to prevent.

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
wrong account, no error. Close that gap by comparing hashes, never credential
values:

```bash
# runs on: host, repo root — exits 0 only if the lane's live credential pair
# hash-matches the env file you just wrote
python3 scripts/alpaca_onboarding_gates.py credential-match --mode paper
```

(For Live: `--mode live`. The mode resolves everything else — `live.env`, the
`live` slot's genuinely different `ALPACA_CREDENTIAL_LIVE_KEY_ID` /
`ALPACA_CREDENTIAL_LIVE_SECRET_KEY` pair, and `alpaca-live-clerk` — from one
flag, so there is no second place to get the slot mapping wrong. `--env-file`
and `--container` override the defaults if your deployment names them
differently.)

Both the key id and the secret are checked. Each value is hashed where it lives
— the container prints only a digest, never a value, and the host-side value
never leaves the gate's own process — and the gate refuses on three separate
conditions, any one of which exits non-zero: the variable is empty or absent in
the env file, it is empty or absent inside the container, or the two digests
differ. The empty checks are the load-bearing half, not defensive padding: a
credential absent on *both* sides hashes to the same `e3b0c442…` on both sides,
so a gate that only compares digests hands a green check to exactly the operator
it exists for — one who forgot the credential, or whose recreate picked up an
empty env file. In the other direction, the gate parses the env file the way
Compose's `env_file:` does, stripping surrounding quotes and a trailing `\r`, so
a correctly recreated lane whose value happens to be quoted is not refused.

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

The cutover tool never calls Alpaca itself — it is deliberately broker-free,
and `_validate_cutover_broker_state`
(`app/broker/alpaca/clerk/sqlite/cutover.py:698-716`) refuses only on what the
evidence JSON says, never on anything it fetches. So the capture, the flatness
check and the evidence file are all yours, and for Live they are the only thing
standing between a non-flat real-money account and a receipt that claims
otherwise. One command does all three, once per capture:

```bash
# runs on: host, repo root — capture 1 of 2, the one `cutover-initialize` reads
python3 scripts/alpaca_onboarding_gates.py capture-evidence \
  --mode paper --phase initialize --account-id <ACCOUNT_ID>
```

```bash
# runs on: host, repo root — capture 2 of 2, taken after the verified backup;
# `cutover-plan` and `cutover-apply` share this one
python3 scripts/alpaca_onboarding_gates.py capture-evidence \
  --mode paper --phase plan --account-id <ACCOUNT_ID>
```

`--mode live` switches all of it at once: `deploy/fleet/env/live.env`, the
`live` slot's `ALPACA_CREDENTIAL_LIVE_KEY_ID` / `ALPACA_CREDENTIAL_LIVE_SECRET_KEY`
pair, `https://api.alpaca.markets`, and `alpaca-live-clerk`. Live's key/secret
variables are a genuinely different pair, not the same names in a different
file, and the mode flag is the only place that choice is made — there is no
second spot to get it half-right.

Each invocation, in order, and stopping the moment any step refuses:

1. **Reads the mode's credential pair** from the mode's env file and refuses if
   either half is empty. (`grep … | cut -d= -f2-` out of the wrong file finds
   nothing and silently hands an empty credential to an authenticated request.)
2. **Captures three endpoints** — `/v2/account`, `/v2/orders?status=open` and
   `/v2/positions` — and aborts on any non-2xx. The error body is written to
   disk so you can read it, and is never treated as evidence. Three, not two,
   because three is what the real 2026-09-15 ceremony captured, and the third
   is the one its receipt cites.
3. **Gates the account as flat and quiet.** `/v2/positions` must be a JSON array
   of length zero — direct, with no field-name dependency, wrapping the same
   endpoint `app/broker/alpaca/client.py:289-290` already uses, and unable to
   pass on an error body. `/v2/orders?status=open` must likewise be an *array*
   of length zero (`{}` also has length zero, which is how a bare length check
   passes an error object). `long_market_value` and `short_market_value` must
   both read as zero, taken by direct subscript exactly as
   `app/broker/alpaca/adapter.py:211-212` takes them: a missing key is an error
   body, not a zero. And the account number the credentials actually answered
   for must be the `--account-id` you named — the cheapest possible catch for
   "right ceremony, wrong lane's env file".
4. **Retains all three raw responses** under
   `broker_captures/cutover-<UTC date>/` on the lane's artifacts volume — the
   lane's own `BROKER_CAPTURE_DIR` (`compose.fleet.dev.yaml:48`), which is where
   the real run left them. Nothing else copies them there, and
   `_validate_cutover_broker_state` checks `proof_reference` for non-emptiness
   only (`cutover.py:654-655`, `:706-707`) — it never resolves it as a path, so
   an unpopulated reference would go unnoticed by the tool. Override the
   directory with `--capture-dir` if you are re-running on a later date.
5. **Writes the broker evidence file** the CLI reads, in the shape fixed by the
   subprocedure's [§ Broker evidence
   files](alpaca-sqlite-clerk-recovery-and-cutover.md#broker-evidence-files),
   and copies it in beside the captures:

   ```json
   {
     "account_id": "<ACCOUNT_ID>",
     "account_mode": "paper",
     "observed_at_ms": 1800000000000,
     "proof_reference": "broker_captures/cutover-2026-09-15/init-positions.json",
     "positions": {},
     "open_order_ids": []
   }
   ```

   `observed_at_ms` is `int64 ms UTC` (per this repo's temporal-rigor rule),
   taken immediately *before* the first request so the evidence is never
   claimed fresher than it is — `--max-evidence-age-ms` is measured against it.
   `proof_reference` names a **file**, not a directory: the phase's
   `…-positions.json`, which is the capture that directly answers
   `positions: {}`, and the same shape both the subprocedure's example and the
   real ceremony's receipt use. `positions` and `open_order_ids` are
   transcriptions of the gates in step 3, which have just proven both — not
   defaults you are entitled to assume.

Finally it prints the `--broker-evidence <path>` to hand
`manage_alpaca_sqlite_clerk`, already inside the container.

If any of this refuses, the account is not a candidate for the fresh-account
path this runbook documents. Stop and escalate; do not hand-edit a non-empty
result away.

### Two things that were proven the hard way on 2026-09-15

Both of these were hit for real during this ceremony's first live run, and
both will bite you again if you skip this paragraph.

1. **A WAL/SHM sidecar refuses planning — and the fix is a checkpoint,
   never file deletion.** A lane whose Clerk container stays up between
   attempts can accumulate a stale, 0-byte `clerk.db-wal` + `clerk.db-shm`
   pair, left behind by a connection that exited without a clean close.
   `cutover-plan` refuses to run against it (the refusal text itself says
   "remove no files manually" — believe it). There is no CLI subcommand for
   this: `manage_alpaca_sqlite_clerk` has none, and the only callers of the
   checkpoint helper are internal, non-operator-facing scripts. **With nothing
   holding the database open** — no governed bot running against this account,
   which on a brand-new lane is trivially true — run:

   ```bash
   # runs on: host, repo root
   python3 scripts/alpaca_onboarding_gates.py wal-checkpoint \
     --mode paper --account-id <ACCOUNT_ID>
   ```

   SQLite removes the sidecar files itself once the checkpoint succeeds and the
   connection closes. Never `rm` a `-wal` or `-shm` file by hand. Two things
   this gate exists to refuse, neither of which the obvious one-line
   `sqlite3.connect(...)` can:

   - **`sqlite3.connect()` creates the file.** A typo'd `<ACCOUNT_ID>` or
     artifacts root silently writes a new 0-byte `clerk.db` into the custody
     volume, and `ClerkSqliteRepository.initialize` then refuses the account
     outright because `clerk.db` already exists (`repository.py:309-310`,
     `AlreadyInitialized` at `:112-113`) — so the "safe alternative to `rm`"
     bricks the very `cutover-initialize` you are about to run, and the only
     obvious recovery is the `rm` this whole paragraph forbids. The gate opens
     the database read-write through a `file:…?mode=rw` URI behind an explicit
     existence check, so it creates nothing and tells you the path was wrong.
   - **`(0, …)` is not a success criterion.** An empty database returns
     `(0, -1, -1)` — indistinguishable from a real checkpoint under a
     "first element is zero" rule, and exactly what the typo above produces.
     The gate requires `(0, n, n)` with both frame counts non-negative.

   A busy result (`(1, n, n)`; SQLite does not raise for it) means something
   still holds the database open. The remedy, in this order: stop every
   governed bot for this account — the subprocedure's own boundary — and if the
   lane's Clerk process is itself the holder, which is the usual case in the
   scenario this gotcha is about (a lane whose Clerk container stays up between
   attempts), `podman restart alpaca-paper-clerk` and re-run the gate. That is
   safe here: checkpointing is crash-safe, the restart does not touch the
   database's contents, and a pre-activation lane composes no SQLite authority
   to reopen it with. A busy result that survives that is an incident to
   escalate, not something to retry blind — and still never a reason to delete
   a sidecar.
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
prints, reusing that same second evidence file for both — each capture prints
the in-container `--broker-evidence` path to pass. Use the container form from
the top of this section throughout. Review every receipt as it's produced; none
of these steps are silent.

## 7. Verify the account is genuinely live

Three independent checks, all from the host. Read the first two in light of
the third — they are necessary post-§4 facts, not proof §6 ran:

```bash
# runs on: host, repo root
curl -sS --fail-with-body localhost:8000/api/broker-clerks \
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
names the composed authority (`app/broker/alpaca/clerk/fleet_boot.py:558-563`),
and it reads differently by mode for the same "§4 done, §6 not done" state:

- `real_paper` / `real_live` — activated; §6 completed for this account.
- **`unavailable`** — the reading for a **Paper** lane that completed §4 and
  has not yet run, or not yet completed, §6. It is also the map's fallback for
  any authority kind it doesn't recognize (`.get(authority_kind, "unavailable")`).
- **`shadow`** — the reading for a **Live** lane in that same state, not
  `unavailable`: a Live account with no activation record is routed to
  `select_shadow_clerk_runtime` (`active_authority.py:174-184`), which composes
  `authority_kind="shadow"` (`shadow_authority.py:211`). This is §5's asymmetry
  showing up in the directory, and it is why a Live lane can look "done" from
  here while holding no real custody at all.
- `synthetic` — a distinct state the same map can emit (`fleet_boot.py:561`);
  it is not an outcome of this runbook, and seeing it after a cutover means
  something composed a stand-in authority rather than the real one.

Seeing `shadow` for a **Paper** account is itself a bug report — Paper has no
shadow fallback. Seeing `unavailable` where you expected `real_paper`, or
`shadow` where you expected `real_live`, means §6 has not completed; go run it,
don't re-run §4.

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

**Where the generation actually moves, and where it doesn't.**
`effective_binding_generation` advances in `effective_acknowledged`
(`app/broker_configuration/selection.py:71-92`) and only when an
acknowledgement changes the effective `(profile, revision, account)` tuple —
its own docstring is explicit that a stage, a refused Apply, an ordinary
restart, and an Apply that re-acknowledges the same tuple all leave it alone.
So it moves at **§4**'s Apply-plus-restart, `0 → 1` on a first onboarding, and
across the whole of **§6 it moves by zero**. Do not read a cutover as failed
because the number didn't change; expect it not to.

On a second onboarding of the same lane it will legitimately read `2`, `3`, and
so on, for a reason worth knowing: the §8 Paper reset clears
`effective_profile_id`, `effective_revision` and `effective_account_id` but
leaves `effective_binding_generation` intact (`paper_reset.py:96-111`), so §4's
next Apply changes the tuple — from cleared back to set — and takes the next
number. The rule is therefore: **after §4 it must be ≥ 1 and one higher than
its pre-Apply value; after §6 it must be unchanged.** A different counter,
`authority_generation`, does advance during the cutover
(`cutover.py:275-281`), but it lives in the activation record on the lane's
own volume and appears nowhere in this payload — do not reach for it here.
Treat `effective_binding_generation` and `lifecycle_state` as necessary signals
that a bind happened, never as sufficient proof that `cutover-apply` succeeded
— that proof is `authority_state` above, alone.

**The desk shows it.** Open `http://localhost:4200/brokers/alpaca` — the
new lane appears in the directory as `ready`, and its account page (via the
lane's own clerk link) shows a real account snapshot: balances, buying
power, and (for a genuinely fresh account) zero positions and zero open
orders, pulled live from Alpaca rather than placeholder data.

If any of the three disagrees with the others — directory says `ready` but
the desk hasn't updated, or the desk shows a real account snapshot while
`authority_state` still reads `unavailable` — do not paper over it by
re-running the ceremony. Stop, preserve
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
- `scripts/alpaca_onboarding_gates.py` — the gates §3, §6 and its WAL gotcha
  run, with their regression tests in `scripts/test_alpaca_onboarding_gates.py`
