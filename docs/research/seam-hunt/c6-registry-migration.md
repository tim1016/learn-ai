# C6: is the fleet registry v6 to v7 migration safe on the live coordinator?

**Ticket:** #2277, part of map #2276 (seam C6: registry schema migration and restore).
**Baseline:** every `file:line` below is at `a14f1df1` (master, 2026-09-23) unless it names another commit.
**Method:** static trace only. Nothing was run against the live coordinator, its control volume, a lane, or a broker. The two existing suites for this seam were run on the host venv and pass: `tests/broker/fleet/test_schema_migration.py` and `tests/broker/fleet/test_registry_recovery.py` (28 passed).

## Verdict: needs a rehearsal (a cheap one) before the restart

The migration itself is sound. It runs two DDL statements in one `BEGIN IMMEDIATE` transaction. It completes before the coordinator's HTTP listener opens. It rolls back cleanly and is idempotent on re-entry. It cannot lose, reshape or reinterpret a row.

It has one refusal condition, and nobody has checked the live registry against it. If any clerk holds **two or more non-released assignment rows**, the upgrade rolls back. The coordinator then refuses to start, and because `python-service` is both the coordinator and the data plane (`restart: always`), it crash-loops. The lanes keep running their bots, but the operator loses every fleet-routed control.

The v6 code could create exactly that state (see G1). A lane that routes normally today does not prove the registry is clean, because routing reads only `effective` rows. So: **do not restart blind.** Run a read-only pre-flight on a copy first (G1's prototype). If it passes, the restart is safe.

## (a) Trace across the boundary

### Where the migration runs

1. `polygon-data-service` runs `FLEET_ROLE: fleet_coordinator` with `FLEET_CONTROL_DIR: /app/artifacts/fleet` on the named volume `alpaca-fleet-control` (`compose.fleet.dev.yaml:81-116`, volume at `:114`). It bind-mounts the main checkout's `PythonDataService/app` (`compose.fleet.dev.yaml:103`), so a restart runs whatever code the main checkout holds. The base service has `restart: always` (`compose.yaml:95`, service block from `compose.yaml:70`).
2. Both lanes are `clerk_agent` with `FLEET_COORDINATOR_URL` (`compose.fleet.dev.yaml:38-40`). An agent lane never opens the registry. Only the combined role opens it locally (`app/broker/alpaca/clerk/fleet_boot.py:367-379`). The only processes that open the live registry are the coordinator and operator one-shots of `scripts/manage_broker_fleet.py` (`:95`, `:104`, `:368`).
3. Coordinator startup: `_service_lifespan` (`app/main.py:353`) calls `FleetRegistryStore.open(control_dir=...)` at `app/main.py:408`, inside lifespan startup and so before the listener accepts any request.
4. `FleetRegistryStore.open` (`app/broker/fleet/store.py:75-109`) takes the cross-process `advisory_file_lock(db_path)` (`store.py:80`, an `fcntl.flock` on a sibling lock file, `app/utils/advisory_lock.py`). It connects with `isolation_level=None` (`store.py:82`), applies the WAL, `synchronous=FULL` and `foreign_keys` PRAGMAs, refusing non-WAL (`schema.py:633-651`), then calls `_establish`.
5. `_establish` (`store.py:112-173`) reads `fleet_meta.schema_version` (`store.py:134-142`). It refuses a newer version (`store.py:143-148`) and refuses an unchained one (`store.py:149-155`). Otherwise it calls `schema.migrate_schema(conn, from_version=stored)` (`store.py:156`), stamps `updated_at_ms` in autocommit (`store.py:157-160`), and runs `PRAGMA quick_check` (`store.py:166-173`).
6. `migrate_schema` (`schema.py:669-697`) runs `BEGIN IMMEDIATE`, applies each registered step from `from_version` up to `SCHEMA_VERSION = 7` (`schema.py:92`), sets `schema_version = 7`, and commits. Any exception rolls back and re-raises.
7. The v6 to v7 step is two statements (`app/broker/fleet/schema_migrations.py:516-520`, registered at `:538`): `DROP INDEX ix_account_assignments_owner`, then `CREATE UNIQUE INDEX ix_account_assignments_owner ON account_assignments(clerk_id) WHERE state <> 'released'`. The result matches the fresh v7 DDL (`schema.py:275`). The existing test pins that match (`tests/broker/fleet/test_schema_migration.py:709-728`).
8. On failure, `sqlite3.IntegrityError` (a `DatabaseError`) is caught at `store.py:103` and re-raised as `FleetRegistryUnavailable`. `app/main.py:408` does not catch it, so lifespan startup fails and the process exits. The rollback leaves the file at v6 with its rows intact (`test_schema_migration.py:731-757`).

### What the lanes do during the restart window

- A heartbeat is `RemotePresence._post`. A transport error becomes `FleetPresenceError` (`app/broker/fleet/presence.py:367-374`), and so does a 5xx (`presence.py:377-380`). Both are `FleetControlError`s.
- `_beat` catches a `FleetControlError` and calls `_repair_lane_after_refused_beat` (`fleet_boot.py:792-799`). That function absorbs further `FleetControlError`s while the coordinator is down (`fleet_boot.py:1082-1125`), so the beat task survives the outage.
- Sessions persist in `clerk_sessions`, so the restarted coordinator recognizes them. A replaced session re-presents its grant on every later beat (`_reconfirm_grant_if_stale`, `fleet_boot.py:808`).
- A lane-quiet confirmation is also retried each beat (`fleet_boot.py:809`).
- **No heartbeat or confirmation can interleave with the migration inside the coordinator.** The migration finishes before the listener opens, and lanes reach the registry only over HTTP.

### Restore path

- `restore_registry_backup` (`app/broker/fleet/recovery.py:344-397`) validates the manifest hash and metadata, and caps the version at `SCHEMA_VERSION` (or at `D_COMPATIBLE_SCHEMA_VERSION = 2` for `--d-compatible`; `scripts/manage_broker_fleet.py:299-303`). Under the same advisory lock, it writes the closed recovery hold first (`recovery.py:367-379`), then swaps the file in (`recovery.py:389-396`).
- The next `FleetRegistryStore.open` migrates the restored v6 (or v2) file to v7 exactly as above. The migration preserves `registry_id`, which is what the hold is keyed on (`recovery.py:405`), so the hold survives the upgrade.
- The hold is enforced per mutation (`service.py:244-249`, `recovery.py:400-423`), not at open. So the migration always runs on a restored file, whatever the hold says.

## (b) Invariants each side assumes, and whether they hold

| # | Assumed by | Assumption | Guaranteed? |
|---|---|---|---|
| I1 | v7 migration (`schema_migrations.py:514-520`) | No clerk holds more than one non-released `account_assignments` row. | **No.** The v6 `reserve_assignment` inserted a fresh reservation whenever the account had no row, with no per-clerk check (`fa65af71`, the parent of `9cc894c9`: `service.py:1427-1437`). v6 routing refuses only more than one **effective** row (`service.py:2189-2200` at HEAD, same logic at v6). An `effective` row beside a stray `reserved` row routes normally and shows nothing in the directory, which projects only effective rows (`service.py:2596`). The v7 service adds `_require_no_live_assignment` (called at `service.py:1475`, `:1604`, `:2029`; defined `:1661-1675`), but that only guards writes made after the upgrade. |
| I2 | `migrate_schema` | `from_version` is still current when `BEGIN IMMEDIATE` is taken. | **Yes, conditionally.** The version is read outside the SQLite write transaction (`store.py:134` vs `schema.py:678`) but inside the advisory flock (`store.py:80`). Every `open` takes that flock. A writer that bypasses it (a raw `sqlite3` shell) is not serialized. For v6 to v7 a re-run is harmless (DROP and CREATE of the same index). |
| I3 | Operator and runbooks | `backup-registry` captures the registry as it stands. | **No, when the tool is newer than the file.** `_backup_registry` opens through `_service`, which calls `FleetRegistryStore.open` (`scripts/manage_broker_fleet.py:93-100`, `:278-294`). That migrates the file in place before `create_registry_backup` snapshots it (`recovery.py:180-212`). See G2. |
| I4 | A code rollback | The previous build can reopen the registry. | **No, once migrated.** A v6 build refuses a v7 file (`store.py:143-148`, the same check at v6). The only way back is to restore a v6 backup. |
| I5 | Recovery hold | The hold's `required_clerk_ids` covers every lane holding authority. | **Partly.** It lists only lanes that were **effective** at capture (`recovery.py:197-209`). A lane still `reserved` at capture but confirmed afterwards is not required, and the empty-inventory closeout is allowed when nothing is effective (`recovery.py:449-458`). See G5. |
| I6 | Lanes | The coordinator answers again after a restart, with their sessions intact. | **Yes.** Presence errors are typed and absorbed (see "What the lanes do" above). Sessions are persisted rows. Charted-fixed neighbour: #2259. |
| I7 | Coordinator startup | The coordinator either opens a sound registry or refuses loudly. | **Yes.** It fails closed (`store.py:103-109`, `:166-173`). The cost of that refusal is G1. |

## (c) Named suspected gaps

**G1: a stray second live assignment bricks the coordinator restart.** Provisional severity **P1**. The worst reachable consequence: while the coordinator crash-loops, live bots keep trading and every fleet-routed operator command (stop, flatten, cancel) fails. The only fallback is the broker's own UI. If that fallback is judged unavailable, this is P0.
- *Hypothesis.* The live v6 registry holds at least two rows with `state <> 'released'` for one `clerk_id` (for example an effective account plus a mistyped or duplicate `reserve`). The v7 `CREATE UNIQUE INDEX` then fails, `FleetRegistryStore.open` raises `FleetRegistryUnavailable`, `app/main.py:408` aborts startup, and `polygon-data-service` crash-loops under `restart: always`.
- *Prototype.* Copy the live registry **with the SQLite online backup API or the `sqlite3 .backup` command, never `manage_broker_fleet backup-registry` from new code** (see G2) into a scratch control dir. Then:
  - (1) `SELECT clerk_id, count(*) FROM account_assignments WHERE state <> 'released' GROUP BY clerk_id HAVING count(*) > 1` must return no rows.
  - (2) `SELECT schema_version FROM fleet_meta` confirms the start version is really 6.
  - (3) `FleetRegistryStore.open(control_dir=<scratch>)` on the host venv at the restart's exact SHA must reach `schema_version == 7` and pass `quick_check`.
  - If (1) returns rows, the fix is an operator release of the stray row on the **v6** build before the restart, not hand SQL (`trg_account_assignments_no_delete` forbids a delete).
- *Needs paper account:* no.

**G2: `backup-registry` from newer code migrates before it snapshots.** Provisional severity **P2**. Proven by reading, and it does not reach an order.
- *Hypothesis.* Running `manage_broker_fleet backup-registry` with v7 code against a v6 control volume upgrades the live file first (`scripts/manage_broker_fleet.py:278-281` into `store.py:156`). There is then no v6 rollback artifact. If the old coordinator is still up, this is also an unannounced live migration underneath it.
- The dev-posture and account runbooks run this command from an image (`docs/runbooks/fleet-dev-two-lane-posture.md:128-132`, `docs/runbooks/add-an-alpaca-account.md:203-207`). Whether that migrates depends on how old the image's baked code is, which nobody can tell by looking.
- *Prototype.* Build a v6 registry with the existing `_build_v6_registry` helper (`test_schema_migration.py:163`), run `main(["backup-registry", ...])`, and assert that the manifest's `registry_schema_version` is 7 and the source file is now at v7.
- *Needs paper account:* no.

**G3: a code rollback after the restart costs a full restore ceremony.** Provisional severity **P2**.
- *Hypothesis.* After the upgrade, going back to the v6 build requires `restore-registry` from a v6 backup (I4). That opens a hold which needs `reconcile-registry` for every lane effective at capture (`recovery.py:93-95`, `:477-622`). Every registry write made since the backup is lost: sessions, receipts and lane-quiet confirmations. Routing stays closed until each lane is reconciled.
- *Prototype.* Migrate a scratch copy. Show that the v6-SHA checkout refuses to open it. Restore the v6 backup, then time and verify the reconcile of two synthetic lanes.
- *Needs paper account:* no.

**G4: a restored backup that breaks the v7 invariant cannot be recovered with v7 tools.** Provisional severity **P3**.
- *Hypothesis.* Every recovery CLI verb opens through `FleetRegistryStore.open`, and so through the migration: `reconcile-registry` and `closeout-empty-registry` (`scripts/manage_broker_fleet.py:336-390`). A restored v6 backup holding two live rows for one clerk therefore refuses every recovery verb. The ceremony deadlocks until a v6 build releases the stray row.
- *Prototype.* Use the fixture from `test_a_v6_registry_with_two_live_assignments_for_one_clerk_refuses_to_upgrade`, wrapped in a backup and manifest. Run `restore-registry` and then `reconcile-registry`, and observe `FleetRegistryUnavailable` on both.
- *Needs paper account:* no.

**G5: a backup that predates a lane's confirmation reopens mutations on a stale `reserved` row.** Provisional severity **P2**.
- *Hypothesis.* A backup captured while lane L was `reserved` leaves L out of `required_clerk_ids` (`recovery.py:201-208`). After a restore:
  - with no other effective lanes, `closeout-empty-registry` reopens mutations (`recovery.py:449-474`) while L's volume evidence says `effective`;
  - L's next beat re-confirms, which self-corrects (P2);
  - but an operator `release` in that window records `lane_confirmation: absent`, and ADR 0063's own "what stays open" note says an account in that state cannot be re-reserved by any lane.
- *Prototype.* Take a backup with L reserved, then confirm L. Restore, close out as empty, release the account before L beats, and assert the account can no longer be re-reserved.
- *Needs paper account:* no.

**G6: an old coordinator writing under a newer migrator gets a misleading refusal.** Provisional severity **P3**.
- *Hypothesis.* If a v7 one-shot migrates while the v6 coordinator still runs, a v6 `reserve` for a clerk that already holds a live row hits the new unique index. The v6 code reports that as "reserved concurrently by another clerk" (`fa65af71:service.py:1510-1519`). The refusal fails closed but names the wrong cause.
- *Prototype.* Open a v6 store from the v6 SHA, migrate the same file from a second v7 process, then reserve a second account for the same clerk through the v6 service.
- *Needs paper account:* no.

## (d) Defects proven by reading alone

- **G2 is proven:** `backup-registry` migrates before it snapshots (the code path is fully static: `manage_broker_fleet.py:93-100, 278-294` into `store.py:75-160`). Its severity is P2: it touches no order, and the manifest's `schema_version: 7` shows what happened. Per the map's policy it is listed here and gets no bug issue.
- **No P0 or P1 defect is proven.** G1's trigger depends on data in the live registry, which this ticket may not read. Its prototype is the pre-restart rehearsal.

## What the restart needs, in order

1. Take a v6 rollback copy with the SQLite backup API (not the v7 CLI), while the coordinator is up or stopped.
2. Run G1's three checks on a scratch copy of it, at the restart's exact SHA.
3. If they are clean, restart `polygon-data-service`. The migration runs before the listener opens, and the lanes ride out the gap through their typed presence retry.
4. If they are not clean, release the stray row on the v6 build first.
