# Fleet D recovery, backup, and rollback posture

**Status:** Delivery D operating posture. It prepares recovery and rollback; it does not claim they have been exercised. Delivery E owns the required exercised backup/restore, reassignment, registry-recovery, and compatible-rollback evidence.

**Authority:** [ADR 0062](../architecture/adrs/0062-broker-clerk-fleet-control-plane.md), the [multi-broker Clerk PRD](../prds/2026-09-12-multi-broker-clerk-control-plane.md), and the existing [Alpaca SQLite Clerk recovery and cutover procedure](alpaca-sqlite-clerk-recovery-and-cutover.md). Use the latter for the provider-owned custody procedure. This document adds the fleet boundary; it never replaces a provider ceremony.

## Non-negotiable stop boundary

Host-only ceremonies are required for volume remount, restore, reassignment, endpoint change, retirement, coordinator registry recovery, and Live arming. The browser may inspect lane evidence but cannot perform any of those actions.

Before a restore, reassignment, or lane rollback:

1. Stop the affected Clerk service and every writer to its own lane volume.
2. Preserve process-stop evidence and the lane's current fleet directory/assignment observation in the incident record.
3. Verify the other lane is not mounted, stopped, copied, or modified as part of this work.
4. Preserve the original opaque Clerk ID, volume ID, account-assignment generation, and deployment-attestation evidence. Do not mint replacement identities to work around a partial operation.
5. Follow the provider-specific offline recovery procedure for custody/lease work. An unreadable or expired-looking lease is not proof that the process stopped.

If any condition is missing, stop. Do not use `combined`, a browser operation, a copied marker, or a shared volume as a workaround.

## Backup posture

Back up the coordinator control volume, Paper lane volume, and Live lane volume as three independently named artifacts. Capture the following nonsecret manifest next to each backup:

| Backup artifact | Manifest must identify | May contain | Must never contain |
|---|---|---|---|
| Coordinator | Registry ID, schema version, deployment namespace, source volume identity, capture UTC ms, hash/immutable storage reference | Fleet registry and routing evidence | Lane profiles/custody/order/fill/position/arming data; broker or transport credentials |
| Paper lane | Clerk ID, volume ID, broker, attestation, account-assignment generation, capture UTC ms, hash/reference | Paper profiles, custody, lease, receipts, runner state, recovery material | Live files, Live credentials, coordinator registry source-of-truth |
| Live lane | The same identity fields, independently | Live profiles, custody, lease, receipts, arming evidence, runner state, recovery material | Paper files, Paper credentials, coordinator registry source-of-truth |

Do not copy SQLite database, WAL, or SHM files from a host while a Clerk can own the authority. Use the existing Clerk procedure's same-filesystem tooling and stop boundary. A backup is a recovery input, not proof it is safe to restore.

## Restore posture

Restoring any fleet artifact is a safety event:

1. Restore only the named artifact to its original named volume and its original deployment namespace. Never use a restore to seed another lane.
2. Verify actual orchestrator volume source, mount destination, backend marker, and host attestation before starting a process.
3. For a lane restore, validate its own Clerk/volume/registry identity and provider custody evidence under the provider's offline procedure. Do not copy a confirmation record or arming ledger from the other lane.
4. For a registry restore, keep routing and new assignment creation closed. A restored registry may be older than durable lane evidence. Reconcile each original lane's durable confirmation evidence and deployment membership by a host ceremony before routing can reopen.
5. Re-run the lane's volume verification and record the refusal/pass output. A mismatch, clone detection, unknown assignment, or stale generation is a hold, not an opportunity to release an assignment.

The acceptable offline behavior during a coordinator outage is narrow: an original, verified Clerk may recover only its already-confirmed exact last-effective binding. First enrollment, a changed binding, assignment creation/release/reassignment, and browser fleet commands remain closed.

## Reassignment posture

Assignment ownership never expires on heartbeat loss, coordinator restart, or lease expiry. To change an assignment, the host ceremony must prove the old agent and volume are offline, credentials are isolated, and provider-specific obligations are clear; it must use the expected assignment generation. If the evidence is stale or incomplete, reassignment refuses. This is intentional split-brain prevention.

## Rollback posture

Choose the narrowest safe rollback. A Paper fault normally stops only `alpaca-paper-clerk`; it must not restart, retarget, or modify Live. A coordinator rollback after two lanes exist exposes each original lane only through its own independent single-Clerk fallback route after compatible-binary/schema evidence is reviewed. Keep the fleet registry and audit history intact. A fleet rollback invocation must still suppress the legacy combined role; starting it as an accidental fourth authority is not a rollback.

Rollback must never:

- merge Clerk volumes or writable roots;
- copy custody, profiles, confirmations, receipts, credentials, or arming ledgers between Paper and Live;
- delete registry rows, release an account assignment, or mint a replacement Clerk identity implicitly;
- return an enrolled production volume to `combined` mode;
- bypass existing Live envelope, arming, custody, risk, capability, binding, idempotency, or outcome-reconciliation gates.

The Live safety invariant survives every rollback state: the coordinator has no Alpaca execution credential or lane custody (its existing research/data-lake dependencies remain separate), assignment ownership never expires into takeover, and a Live mutation is admitted only by the original provider-owned Live authority after its host-only arming and all existing gates. An older binary is eligible only after its schema compatibility is proven. A failed rollback attempt remains an incident: leave routing closed where required, retain the artifacts, and escalate rather than attempting a destructive repair.

## Evidence handoff to Delivery E

D records the backup manifests, exact stop/restore/reconciliation plan, compatible binary candidate, and named operators. E must attach actual transcripts for:

- restoring one lane without mounting or writing the other;
- restoring an older registry with routing closed until reconciliation;
- proof-driven reassignment with no heartbeat-expiry takeover;
- coordinator and lane rollback using schema-compatible artifacts; and
- validation that Live arming/envelope evidence remains lane-local and unchanged.

Until those transcripts exist, state `recovery posture documented; not operationally qualified`. D's isolated actual-role Compose qualification may exercise selected rollback-shaped probes, but it cannot replace E's actual restore, reassignment, registry-recovery, or rollback evidence.
