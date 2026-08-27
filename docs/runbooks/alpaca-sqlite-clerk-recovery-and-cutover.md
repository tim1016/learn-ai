# Alpaca SQLite Clerk recovery and cutover subprocedure

**Status:** Implemented tooling; human account activation remains gated by issue #1383.

**Authority:** This is the focused Alpaca SQLite subprocedure incorporated by
`docs/broker-v2-operator-manual.md`. It does not authorize a cutover by itself and
does not replace that manual.

The tool is broker-free: it verifies operator-captured JSON evidence but never calls
Alpaca. There is no force flag. Run commands from `PythonDataService/` and replace the
examples with the exact account, Clerk-artifact, and runner-artifact paths under
review. In the default compose layout those two roots are
`artifacts/alpaca_clerk/` and `artifacts/`; do not silently substitute one for the
other.

## SQLite WAL storage boundary

SQLite WAL requires every process that opens an authority database to share one
host locking and shared-memory domain. The default Podman compose layout therefore
masks `/app/artifacts/alpaca_clerk` with the VM-local `alpaca-clerk-data` named
volume; it must not run from the parent macOS `virtiofs` bind mount. Startup refuses
known remote/FUSE filesystem types before opening or creating `clerk.db`.

The Compose volume is external and startup also requires the regular-file marker
`/app/artifacts/alpaca_clerk/_compose_volume_ready`. This makes first use an explicit
operator ceremony instead of allowing Compose to create an empty volume that masks a
previous host authority tree.

Before the first startup after adopting this layout:

1. stop the data service and every Clerk writer;
2. create the volume explicitly with
   `podman volume create learn-ai-alpaca-clerk-data`;
3. if `/app/alpaca_clerk_legacy` contains the prior host tree, copy that complete tree
   into `/app/artifacts/alpaca_clerk` from a one-shot `python-service` container while
   the service is stopped. Never copy only `clerk.db`; the registry, activation ledger,
   mirror, WAL, and SHM are one authority set;
4. for every established account, run `verify` below. If verification fails, leave the
   marker absent and use `rebuild` or the documented cutover initialization with fresh
   broker evidence; and
5. only after every retained account agrees with its finalized mirror head, create
   `_compose_volume_ready` inside the named volume. A deliberately empty first install
   may create the marker only after confirming the separately mounted legacy tree is
   empty. Discarding an existing tree requires the reset/cutover proof ceremony; an
   empty marker is not deletion authority.

```bash
podman compose run --rm --no-deps python-service \
  python -m scripts.manage_alpaca_sqlite_clerk \
  --artifacts-root /app/artifacts/alpaca_clerk \
  --account-id PA-EXAMPLE \
  verify
```

The verified command returns the bound account, generation, database identity,
transition count, and mirror-head hash. To seal the already-verified volume:

```bash
podman compose run --rm --no-deps --entrypoint /bin/sh python-service -c \
  'test ! -L /app/artifacts/alpaca_clerk/_compose_volume_ready && \
   : > /app/artifacts/alpaca_clerk/_compose_volume_ready'
```

Do not open or copy a live authority database, WAL, or SHM file from the macOS host.
In the default compose layout, run the commands in this document inside a one-shot
`python-service` container so recovery tooling and the service use the same Linux
host and named volume. For example, after satisfying the stop boundary below:

```bash
podman compose run --rm --no-deps python-service \
  python -m scripts.manage_alpaca_sqlite_clerk \
  --artifacts-root /app/artifacts/alpaca_clerk \
  --account-id PA-EXAMPLE \
  rebuild
```

Use `/app/artifacts` for runner evidence arguments and output files that must remain
visible on the host. Native non-container deployments may use the direct
`.venv/bin/python` examples below only when the Clerk root is on a local filesystem
and every SQLite connection runs on that same host. Never alternate host and
container SQLite clients against one WAL authority.

The `.venv/bin/python` in those examples is `PythonDataService/.venv`. Provision it
with `./bootstrap-host-venv.sh` from the repo root; it installs the same
heavy + light + dev requirement set CI does.

## Stop boundary

Online backup is the only ceremony allowed while the Clerk is running. Before restore,
mirror rebuild, reset, or cutover:

1. stop every governed bot;
2. stop the process that can own the account's SQLite execution lease;
3. verify no second service process can reopen the account; and
4. preserve the operator's process-stop evidence with the incident/cutover record.

The tools reject a readable live lease. Cutover initialization, plan, and apply derive
the account-scoped roster from the runner account registry plus both legacy account bot
layouts; the latter is the read-only source for pre-registry deployments. A bot governed
by another account cannot satisfy or block this account's roster. Every governed Alpaca
binding must have complete canonical runner evidence, `STOPPED` desired state,
`OFF_DUTY`/`RETIRED` lifecycle state, no active run, and a typed durable terminal outcome.
The tools content-hash each complete bot artifact directory and apply rechecks the exact
roster. Freeze **all** writers to the runner `live_state/` tree
from plan through apply, including lifecycle, indicator-state, log, and editor processes.
Any write changes the planned evidence, so apply is expected to refuse and the operator
must capture fresh broker evidence and create a new plan. There is no caller-authored
empty-list bypass.

A corrupt database may make its lease row
unreadable, so a database error is **not** proof that the process stopped. In that case,
the independent process-stop check above is a mandatory precondition and the recovery
command requires a fresh account-bound evidence file:

```json
{
  "account_id": "PA-EXAMPLE",
  "observed_at_ms": 1800000000000,
  "proof_reference": "PythonDataService/artifacts/incidents/2026-08-07/process-stop-proof.json"
}
```

Pass it to `restore`, `rebuild`, or `reset` as
`--process-stop-evidence /app/artifacts/incidents/2026-08-07/process-stop-proof.json`.
Store the host copy at
`./PythonDataService/artifacts/incidents/2026-08-07/process-stop-proof.json`. The default freshness
window is 120 seconds; a reviewed ceremony may tighten it with
`--max-process-stop-evidence-age-ms`. The proof is consulted only when the authority
lease cannot be read, and its reference is copied into the recovery receipt. A readable
unexpired lease always refuses recovery regardless of this file. Never remove a WAL,
SHM, database, mirror, activation record, or quarantine file by hand.

## Broker evidence files

Cutover consumes a freshly captured paper-account evidence file. Timestamps are Unix
epoch milliseconds UTC. `proof_reference` identifies the separately retained Alpaca
response or operator evidence; it is not an API key.

```json
{
  "account_id": "PA-EXAMPLE",
  "account_mode": "paper",
  "observed_at_ms": 1800000000000,
  "proof_reference": "incident/2026-08-06/alpaca-account-and-open-orders.json",
  "positions": {"SPY": 0.0},
  "open_order_ids": []
}
```

The cutover account must match, `account_mode` must be exactly `paper`, every position
must be finite and flat within the canonical `POSITION_QTY_EPSILON`, and the open-order
list must be empty. Cutover derives and hashes the runner roster itself.

Reset uses a distinct recovery proof with the same fields except `account_mode`, which
is omitted because reset preserves the authority's existing deployment scope rather
than authorizing a paper/live scope change. Reset still requires one `--expected-bot`
and matching `--stopped-bot` argument per bot.

## Verified online backup

```bash
.venv/bin/python -m scripts.manage_alpaca_sqlite_clerk \
  --artifacts-root /absolute/artifacts/alpaca_clerk \
  --account-id PA-EXAMPLE \
  backup
```

The command uses SQLite's online backup API, verifies account/generation/database
identity, schema, `integrity_check`, transition sequence and hash chain, fsyncs the
snapshot and manifest, then publishes the bundle atomically beneath
`accounts/alpaca/PA-EXAMPLE/verified-backups/`. An interrupted `.incomplete-*` bundle
is retained as evidence and never replaces `latest.json`.

## Offline v8-to-v9 custody-subject upgrade

Schema v9 introduces durable custody subjects: every existing strategy becomes
one `BOT` subject, while future manual tickets use a distinct
`MANUAL_OPERATOR` subject and never a pseudo-bot or pseudo-run. This is an
offline ceremony, not a startup migration. Startup refuses a v8 authority
until the ceremony completes.

1. Stop every data-plane process that could hold the account's execution
   lease. Capture a fresh account-bound process-stop evidence file; the v9
   ceremony accepts evidence no older than 60 seconds.
2. Run `verify`. Preserve its output with the change record.
3. Run `upgrade-v9` from the same local filesystem/volume as the authority.
   It re-verifies the v8 source, creates a verified backup, rebuilds a staged
   v9 database from finalized mirror records, proves journal and projection
   parity, checkpoints the stopped source WAL into one verified source file,
   rechecks that the source did not change, and then atomically swaps only the
   verified stage into place.
4. Run `verify` again and preserve the completed receipt under
   `offline-v9-upgrades/`. Do not resume the writer until this succeeds.

```bash
.venv/bin/python -m scripts.manage_alpaca_sqlite_clerk \
  --artifacts-root /absolute/artifacts/alpaca_clerk \
  --account-id PA-EXAMPLE \
  upgrade-v9 \
  --process-stop-evidence /absolute/incidents/process-stop-proof.json
```

If a proof fails before publication, the original v8 authority remains
selected, the staged database is retained only for forensics, and a
`.failed.json` receipt records the reason and the verified backup reference.
Immediately before publication the ceremony fsyncs a matching
`.prepared.json` receipt. If the process is interrupted after the swap but
before its completed receipt, leave the authority stopped and rerun
`upgrade-v9` with fresh stop evidence: it verifies the selected v9 file against
that prepared receipt and writes the completion receipt without a second swap
or a new transition. A completed ceremony is idempotent and returns its prior
receipt. Never alter `control_meta` or copy the database by hand.

If an immediate rollback is required before any new transition is accepted,
use the receipt-bound command below. It may restore only the exact verified v8
bundle the upgrade recorded; the general restore protections still reject a
backup behind the mirror head.

```bash
.venv/bin/python -m scripts.manage_alpaca_sqlite_clerk \
  --artifacts-root /absolute/artifacts/alpaca_clerk \
  --account-id PA-EXAMPLE \
  rollback-v9 \
  --process-stop-evidence /absolute/incidents/process-stop-proof.json
```

## Restore a verified snapshot

Restore accepts only a direct, non-symlink child of this account's
`verified-backups/` directory. The snapshot and manifest must be regular files, their
hashes and verification receipt must match, and their generation/database identity
must equal the current registry. The snapshot must also equal the current finalized
mirror head; restore is not a historical rollback mechanism.

```bash
.venv/bin/python -m scripts.manage_alpaca_sqlite_clerk \
  --artifacts-root /absolute/artifacts/alpaca_clerk \
  --account-id PA-EXAMPLE \
  restore \
  --bundle /absolute/artifacts/alpaca_clerk/accounts/alpaca/PA-EXAMPLE/verified-backups/backup-g1-...
```

The old DB/WAL/SHM are moved beneath `recovery-preserved/`, never overwritten. The
candidate is verified before installation and a receipt is written beneath
`recovery-receipts/`. If the bundle is wrong-account, wrong-generation, tampered,
outside the account backup root, symlinked, incomplete, or behind the mirror head, stop.

## Rebuild from the append mirror

Use rebuild when the database is unavailable or corrupt and the write-only mirror is
intact. Rebuild validates the mirror `IDENTITY` record, account, authority generation,
database identity, contiguous sequence, hash chain, and finalized prepare/finalize
fence before moving the old DB files.

```bash
.venv/bin/python -m scripts.manage_alpaca_sqlite_clerk \
  --artifacts-root /absolute/artifacts/alpaca_clerk \
  --account-id PA-EXAMPLE \
  rebuild
```

If the corrupt database prevents the command from reading its lease, repeat the
command with the fresh process-stop evidence described above. Do not supply that file
as a substitute for actually stopping the process.

A tampered, gapped, prepare-only, substituted-account, or wrong-generation mirror is
not recoverable by this command. Preserve the evidence and escalate to reset review.

## Reset to a new authority generation

Reset is the last recovery path. It requires fresh, matching, flat and order-free
broker proof plus a complete stopped-bot roster. It preserves DB/WAL/SHM/mirror,
creates an empty database and bound mirror for generation `N+1`, records provenance,
and appends the established-generation record. All generation-`N` control IDs become
invalid. An already activated account receives a new generation-bound activation
record; reset never falls back to legacy JSONL.

```bash
.venv/bin/python -m scripts.manage_alpaca_sqlite_clerk \
  --artifacts-root /absolute/artifacts/alpaca_clerk \
  --account-id PA-EXAMPLE \
  reset \
  --broker-evidence /absolute/evidence.json \
  --max-evidence-age-ms 30000 \
  --expected-bot bot-a --stopped-bot bot-a
```

Exposure, open orders, a stale/future proof, roster mismatch, live lease, non-regular
authority file, or symlink refuses reset before any authority file is moved.

## Human cutover: initialize, plan, then apply

This section is tooling documentation for #1383, not permission to run it against a
real paper account. The SQLite process must be cleanly stopped and checkpointed.

### Establish the inactive generation

A never-established legacy account first needs one evidence-gated, inactive SQLite
generation. `cutover-initialize` requires fresh broker proof and independently verifies
the account-scoped durable runner roster. Concurrent initialization attempts for the same
account are serialized by one account lock. Before the command creates `clerk.db`, the
mirror identity, and the established-generation registry, it durably records a
content-addressed intent binding the exact broker, Alpaca-roster, and legacy-inventory
evidence. It writes **no activation fence**, starts no SQLite sweep, and leaves the legacy
authority selected. An empty
Alpaca roster or empty legacy inventory refuses initialization, which also catches a
mistakenly supplied artifact root.

```bash
.venv/bin/python -m scripts.manage_alpaca_sqlite_clerk \
  --artifacts-root /absolute/artifacts/alpaca_clerk \
  --account-id PA-EXAMPLE \
  cutover-initialize \
  --runner-artifacts-root /absolute/artifacts \
  --broker-evidence /absolute/evidence.json \
  --max-evidence-age-ms 30000
```

Review and retain the initialization receipt, then publish the verified online backup
described above. If receipt publication fails after the registry fence is durable,
rerun the same command with the exact same evidence. A retry that matches the durable
intent remains supported after that evidence's freshness window expires because it can
only complete receipt publication for the already-created, still-unused authority; it
cannot create or activate an authority. The retry resumes only when the
inactive generation-one registry, database, mirror, account, and identity all verify
exactly and the new evidence matches the durable pre-init intent byte for byte; its
receipt path is deterministic and binds the intent hash to the database identity. A
missing intent, changed evidence, or missing, used, substituted, or corrupt component
refuses the retry and requires a separately reviewed recovery change; the existing reset
command is not an escape hatch for an incomplete first cutover initialization. Never
remove the intent, database, mirror, registry entry, or any related file by hand.

### Produce the read-only plan

Capture fresh broker evidence again after the backup. Any WAL/SHM sidecar refuses
planning. `cutover-plan` is read-only: it verifies the exact DB, fresh broker proof,
flat/order-free account, the database-bound initialization intent/receipt, the exact
empty generation-one mirror and its content hash, the
independently derived stopped roster, and content hashes of every actual legacy
authority artifact. It inventories both the broker-scoped
`accounts/alpaca/<account_id>/` files and the historical
`accounts/<account_id>/bots/` layout, then creates a short-lived content-addressed
confirmation token.

```bash
.venv/bin/python -m scripts.manage_alpaca_sqlite_clerk \
  --artifacts-root /absolute/artifacts/alpaca_clerk \
  --account-id PA-EXAMPLE \
  cutover-plan \
  --runner-artifacts-root /absolute/artifacts \
  --broker-evidence /absolute/evidence.json \
  --max-evidence-age-ms 30000 \
  --output /absolute/cutover-plan.json
```

Review the complete plan and token. Apply rechecks every fact and hash; use a newly
captured broker evidence file only if it is byte-for-byte equivalent to the planned
normalized evidence.

```bash
.venv/bin/python -m scripts.manage_alpaca_sqlite_clerk \
  --artifacts-root /absolute/artifacts/alpaca_clerk \
  --account-id PA-EXAMPLE \
  cutover-apply \
  --runner-artifacts-root /absolute/artifacts \
  --broker-evidence /absolute/evidence.json \
  --max-evidence-age-ms 30000 \
  --plan /absolute/cutover-plan.json \
  --confirmation-token EXACT_TOKEN_FROM_PLAN
```

Before the activation append begins, any failure restores moved legacy artifacts and
leaves SQLite inactive. Once activation append begins, an I/O failure has an uncertain
durability outcome: do not retry blindly. Preserve the quarantine/evidence directories
and activation ledger, then verify them offline. Boot is deliberately fail-closed for a
malformed or mismatched fence.

Successful apply moves only the hashed legacy artifacts into `legacy-quarantine/`,
writes bound broker/quarantine proof, fsync-appends the activation fence, and returns a
durable receipt. On restart, the account selector validates the fence and starts only
SQLite; it never constructs the legacy writer or falls back to JSONL after an activated
startup failure.

## Quarantine the disposable runner catalog after activation

This separate offline ceremony removes post-activation list latency without deleting
evidence. It applies only after SQLite is activated and the stop boundary at the top of
this runbook is proven. The plan reads the verified activation/database identity and the
SQLite strategy registry, then inventories only valid file-backed Alpaca bindings under
the runner `live_state/` tree that are durably bound to the selected account and absent
from that registry. Bindings for another account, registered SQLite instances,
other-broker bindings, and unbound forensic directories are left in place.

The operator must declare both a maximum candidate count and a maximum total byte size.
Planning refuses rather than widening either bound:

```bash
.venv/bin/python -m scripts.manage_alpaca_sqlite_clerk \
  --artifacts-root /absolute/artifacts/alpaca_clerk \
  --account-id PA-EXAMPLE \
  catalog-quarantine-plan \
  --runner-artifacts-root /absolute/artifacts \
  --max-candidates 64 \
  --max-total-bytes 1073741824 \
  --output /absolute/catalog-quarantine-plan.json
```

Review every candidate, its content hash, and the declared bounds. Apply re-verifies the
activation, database, complete SQLite registry, candidate set, byte count, and every tree
hash before moving anything:

```bash
.venv/bin/python -m scripts.manage_alpaca_sqlite_clerk \
  --artifacts-root /absolute/artifacts/alpaca_clerk \
  --account-id PA-EXAMPLE \
  catalog-quarantine-apply \
  --runner-artifacts-root /absolute/artifacts \
  --plan /absolute/catalog-quarantine-plan.json \
  --confirmation-token EXACT_TOKEN_FROM_PLAN
```

The command moves the exact directories beneath
`legacy-catalog-quarantine/<plan-id>/`, writes a prepared/applied manifest plus a durable
receipt, and never deletes them. An in-process failure rolls back newly moved directories;
an interrupted apply can resume from a matching prepared or applied manifest when every
hash still matches. A changed SQLite revision/registry, changed runner artifact, symlink,
wrong token, expired fresh plan, or out-of-bound candidate set refuses the operation.

## Qualification evidence

- CI smoke: `docs/audits/alpaca-sqlite-clerk-qualification-smoke.{json,md}`
- Explicit 1M-row profile: `docs/audits/alpaca-sqlite-clerk-qualification-full.{json,md}`
- Invariant mapping: `docs/references/alpaca-sqlite-clerk-invariant-traceability.md`

No command in this runbook enables live-money trading.
