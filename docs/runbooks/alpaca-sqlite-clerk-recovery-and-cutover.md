# Alpaca SQLite Clerk recovery and cutover subprocedure

**Status:** Implemented tooling; human account activation remains gated by issue #1383.

**Authority:** This is the focused Alpaca SQLite subprocedure incorporated by
`docs/bot-control-operator-manual.md`. It does not authorize a cutover by itself and
does not replace that manual.

The tool is broker-free: it verifies operator-captured JSON evidence but never calls
Alpaca. There is no force flag. Run commands from `PythonDataService/` and replace the
examples with the exact account, Clerk-artifact, and runner-artifact paths under
review. In the default compose layout those two roots are
`artifacts/alpaca_clerk/` and `artifacts/`; do not silently substitute one for the
other.

## Stop boundary

Online backup is the only ceremony allowed while the Clerk is running. Before restore,
mirror rebuild, reset, or cutover:

1. stop every governed bot;
2. stop the process that can own the account's SQLite execution lease;
3. verify no second service process can reopen the account; and
4. preserve the operator's process-stop evidence with the incident/cutover record.

The tools reject a readable live lease. Cutover initialization, plan, and apply also
derive every durable Alpaca binding from the runner's `live_state/` tree and require
`STOPPED` desired state, `OFF_DUTY`/`RETIRED` lifecycle state, no active run, and a
typed durable terminal outcome. They content-hash each complete bot artifact directory and
apply rechecks the exact roster. Freeze **all** writers to the runner `live_state/` tree
from plan through apply, including lifecycle, indicator-state, log, and editor processes.
Any write changes the planned evidence, so apply is expected to refuse and the operator
must capture fresh broker evidence and create a new plan. There is no caller-authored
empty-list bypass.

A corrupt database may make its lease row
unreadable, so a database error is **not** proof that the process stopped. In that case,
the independent process-stop check above is a mandatory precondition. Never remove a
WAL, SHM, database, mirror, activation record, or quarantine file by hand.

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
the durable runner roster. Before it creates `clerk.db`, the mirror identity, and the
established-generation registry, it durably records a content-addressed intent binding
the exact broker, Alpaca-roster, and legacy-inventory evidence. It writes **no activation
fence**, starts no SQLite sweep, and leaves the legacy authority selected. An empty
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
flat/order-free account, the database-bound initialization intent/receipt, the
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

## Qualification evidence

- CI smoke: `docs/audits/alpaca-sqlite-clerk-qualification-smoke.{json,md}`
- Explicit 1M-row profile: `docs/audits/alpaca-sqlite-clerk-qualification-full.{json,md}`
- Invariant mapping: `docs/references/alpaca-sqlite-clerk-invariant-traceability.md`

No command in this runbook enables live-money trading.
