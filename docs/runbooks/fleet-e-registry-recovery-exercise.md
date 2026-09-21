# Fleet E registry recovery and compatible rollback exercise

**Status:** Delivery E host-only recovery procedure. The automated fake-provider exercise proves the ceremony control flow; it is not production qualification. Record a restricted, redacted transcript for every production exercise before claiming `operational rollout complete`.

**Authority:** [ADR 0062](../architecture/adrs/0062-broker-clerk-fleet-control-plane.md), the [multi-broker Clerk PRD](../prds/2026-09-12-multi-broker-clerk-control-plane.md), the [Delivery D recovery posture](fleet-d-recovery-and-rollback.md), and the provider-owned [Alpaca SQLite Clerk recovery procedure](alpaca-sqlite-clerk-recovery-and-cutover.md). This runbook recovers coordinator registry evidence. It does not replace the provider procedure for a Clerk volume, custody, lease, arming, or broker reconciliation.

## Non-negotiable boundary

Run every command from the host against the named coordinator control volume. The browser, coordinator HTTP API, and Clerk agents cannot invoke these ceremonies. Keep Paper and Live on their original volumes and deployment namespace. Never copy a marker, confirmation file, profile, SQLite database, receipt, credential, custody record, or arming evidence across lanes.

The recovery commands do not contain broker credentials and never call a broker, arm Live, submit a command, or replay a routing receipt. A restored registry starts with both routing and assignment mutation closed. It reopens only when every original active Clerk has reconciled its own durable identity and bounded provider summary. A backup with no effective assignment also remains closed until a named host operator records an explicit empty-inventory closeout. Normal provider gates still apply after that.

## 1. Prepare and capture backups

Before a fault, record the image digest, commit, operator, deployment namespace, named-volume source and mount destination for the coordinator, Paper and Live. Stop or otherwise prove offline the role being recovered using the provider procedure. Heartbeat loss is never enough to transfer authority.

Create a fresh restricted directory for each coordinator capture. Do not reuse or overwrite an evidence directory.

```bash
cd PythonDataService
.venv/bin/python -m scripts.manage_broker_fleet backup-registry \
  --control-dir <coordinator-control-root> \
  --backup-dir <restricted-evidence>/registry-<utc-ms>
```

The output pins registry identity, schema version, SHA-256 and the original active Clerk IDs. Store it alongside independently captured provider-owned backups of the Paper and Live volumes. A registry artifact alone cannot restore custody or authorize a Live action.

## 2. Restore a coordinator registry

Do not create an empty registry after a coordinator failure. Stop the coordinator before replacement and record that shutdown in the restricted incident transcript. Choose the exact registry database and manifest pair, inspect their restricted storage reference, then restore it:

```bash
.venv/bin/python -m scripts.manage_broker_fleet restore-registry \
  --control-dir <coordinator-control-root> \
  --backup-dir <restricted-evidence>/registry-<utc-ms>
```

The command verifies the database hash and its embedded registry ID/schema against the manifest. It preserves the previous local database beside the target for incident review, installs the exact backup, and writes a durable recovery hold. It refuses corrupted evidence, an unmatched manifest, or a database newer than the requested compatible binary. Store operations share an exclusive replacement fence and remember the database file identity: if a pre-restore coordinator was not actually stopped, its old SQLite handle refuses every later operation even after lane reconciliation. Restart the coordinator on the restored file before proceeding; never use that fence as permission to skip the stop/restart record.

While the hold is active, all fleet routing, provisioning, endpoint changes, assignment reservation, confirmation, release and reassignment refuse. Agent registration and read-only evidence inspection may continue so an original lane can be prepared for its provider-owned recovery procedure.

## 3. Reconcile every original lane

For each Clerk ID listed by `restore-registry`, prove the original process and volume are the ones named in the incident record. Mount it at its original destination; verify its marker and provider-owned custody evidence; then pass the original lane's bounded provider recovery observation to the ceremony.

```bash
.venv/bin/python -m scripts.manage_broker_fleet reconcile-registry \
  --control-dir <coordinator-control-root> \
  --clerk-id <original-clerk-id> \
  --volume-root <original-lane-root> \
  --provider-summary '{"endpoint_mode":"paper","authority_state":"ready"}'
```

Use the provider's actual `paper` or `live` endpoint mode and its bounded authority state; do not invent a success summary. For every lane, the command requires all of the following to agree before it can mark that lane reconciled:

1. Restored registry ID, durable Clerk ID and immutable volume ID.
2. The original volume marker and mount-attestation evidence.
3. Broker-qualified canonical account and assignment generation.
4. Exact confirmed binding generation, profile/revision tuple, and routing epoch from the Clerk's durable confirmation evidence.
5. A syntactically bounded provider summary confirmation.

If the restored registry lacks an assignment, the ceremony may reconstruct only the same effective assignment owned by that original Clerk and exact durable evidence. It cannot release, transfer, rename, or mint an identity. A conflicting owner, newer registry fact, missing marker, missing evidence or bad summary is a hold: recover a matching backup pair and escalate. Do not force progress by deleting a registry row.

Repeat until output reports `mutations_closed: false`. Starting an agent creates a new routing epoch, so it must perform its ordinary exact-binding confirmation before it becomes routeable. This is intentionally not a command retry and does not replay a routing receipt.

If and only if `restore-registry` reports an empty `required_clerk_ids` list, inspect the restored registry and the restricted incident inventory to confirm that no effective assignment existed at capture. Provisioned or reserved rows are not authority, but an empty list never reopens the registry automatically. Record the closeout explicitly:

```bash
.venv/bin/python -m scripts.manage_broker_fleet closeout-empty-registry \
  --control-dir <coordinator-control-root> \
  --operator <named-operator> \
  --change-ref <restricted-incident-or-change-reference>
```

This command refuses if the restored database contains any effective assignment. It records only the bounded operator name, restricted record reference and UTC-millisecond timestamp; it does not inspect or copy broker credentials. Do not use it instead of reconciling a listed Clerk.

## 4. Exercise same-owner restart and reassignment

For a same-owner restart, restore only the original Clerk volume, preserve its opaque Clerk and volume identities, and have the original agent re-confirm its exact last-effective binding. The assignment remains with that Clerk; do not release it merely because a heartbeat expired.

For a genuine reassignment, ADR 0063 (accepted 2026-09-15) closes the ceremony until further notice: the predecessor must be draining, and no drained lane may be reassigned while restarting during a coordinator outage can resurrect its binding (#2155). The two constraints leave no legal predecessor state, so `reassign-assignment` refuses with `clerk_reassignment_blocked` regardless of the evidence presented. Whole-machine migration (#2151) is the preferred lane move and needs no successor at all; the successor-verification and generation-pinning steps below remain the shape the ceremony will require once #2155 closes:

```bash
.venv/bin/python -m scripts.manage_broker_fleet reassign-assignment \
  --control-dir <coordinator-control-root> \
  --broker alpaca \
  --account-id <canonical-account-id> \
  --expected-generation <observed-generation> \
  --operator <named-operator> \
  --change-ref <restricted-incident-or-change-reference> \
  --successor-clerk-id <original-successor-clerk-id> \
  --successor-volume-root <original-successor-root>
```

When it reopens, the ceremony will release and reserve under a higher assignment generation but will not confirm or route the successor. The successor must pass all existing provider binding, custody, envelope, capability, risk and Live arming gates. Never use this procedure for an automatic takeover. The old `--proof old-clerk-offline-and-obligations-clear` flag is deleted: the proof token was published in the repository that checked it and named nobody, and ADR 0063 Decision 4 replaces it with the bounded `--operator`/`--change-ref` attribution recorded on the released history row.

## 5. D-compatible rollback

Use a registry artifact written at schema version 2 when rolling the coordinator back to the Delivery D fleet-aware binary/configuration. Verify the fallback remains two original independent Clerk volumes and endpoints; `combined` is not a fallback authority.

```bash
.venv/bin/python -m scripts.manage_broker_fleet rollback-d-compatible \
  --control-dir <coordinator-control-root> \
  --backup-dir <restricted-evidence>/registry-d-compatible-<utc-ms>
```

The command refuses a newer schema rather than guessing a downgrade and enters the same reconciliation hold as a normal registry restore. It never remints a Clerk, worker, volume, account assignment or command identity. Retain routing receipts and provider receipts for read-only reconciliation; an `outcome_unknown` is reconciled by the original provider command identity and is never resubmitted by this rollback.

A backup written by this build (schema v3) is refused by `rollback-d-compatible` with exit code `2` — it is newer than what a Delivery D binary can enforce. The D-compatible rollback therefore needs a registry backup captured *before* the v3 upgrade. Retain that pre-upgrade backup as the rollback evidence; once the coordinator has upgraded, no new v2-eligible backup can be produced.

## 6. Required exercise record

Attach a restricted redacted record containing:

- registry manifest hash, schema, registry ID and original Clerk IDs;
- original named volume source/destination and marker/attestation pass or refusal for coordinator, Paper and Live;
- same-owner restart result, explicit reassignment generation and successor still-unroutable result before provider confirmation;
- a deliberately corrupted manifest or confirmation-evidence refusal;
- a deliberately newer-schema refusal;
- restore output showing routing and assignment mutation closed, then each original lane's reconciliation output or the explicit empty-inventory host closeout; and
- D-compatible rollback output plus proof that no combined role, shared volume, credential copy, Live arming change or command replay occurred.

The checked-in tests exercise this control flow only with fake providers and temporary volumes. They do not validate production mounts, actual secrets, real broker state, operator credential isolation, host failure containment or Live command authorization.
