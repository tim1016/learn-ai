# Preserve and restore the data lake

The archive is opt-in. It never enables a new data subscription or changes a
broker. Keep capture and backup on this Mac while the vendor reply is pending.
Moving a backup to an SSD remains a separate owner action.

## Activate preservation

1. Choose a host folder outside both the checkout and the lake. The default is
   `~/learn-ai-lake-backup`. Create it yourself; do not rely on Compose to create
   a missing removable mount. Inside it write `.backup-destination.json` with
   `{"host_destination":"/Users/YOUR_USER/learn-ai-lake-backup"}`. This marker must live on
   the actual destination, not an empty mountpoint underneath a removable disk.
   Use the same absolute path in this marker and the setting in step 2; a `~`
   or relative path is refused, because the container cannot expand it.
2. Set `OPTION_ARCHIVE_BACKUP_HOST_DESTINATION` to that absolute host path and
   `OPTION_ARCHIVE_ENABLED=true` in the data service's environment. Compose
   mounts it at `/lake-backup`, the path `OPTION_ARCHIVE_BACKUP_DESTINATION` names.
   Compose also declares the absolute host checkout and lake paths for
   containment checks across bind-mount aliases.
3. Read `GET /api/option-archive/status` or the Option archive card on Data Lab.
   `POST /api/option-archive/trigger/capture`, `/snapshot`, `/backup`, and `/restore`
   require the normal data-plane control-secret header. They return acceptance;
   read the resulting receipt on the status route. Repeating a running trigger
   joins that job.
4. An exhausted transient capture can be retried explicitly with authenticated
   `POST /api/option-archive/capture-session` carrying `underlying`, the canonical
   `session_ms`, and `retry_exhausted:true`. This scopes recovery to one session.
   A recovery answers 409 while the nightly pass or another recovery holds the
   capture lock; retry it when that one ends.
   Authentication or entitlement failures block that underlying across passes
   and nights. Correct provider access first, then explicitly recover the
   blocked session with `retry_exhausted:true`; only its successful complete
   capture clears the block. A transient recovery failure leaves it blocked.
   A full `OPTION_ARCHIVE_STAGING_MAX_BYTES` budget pauses the pass and appears
   as `capacity_pause` with its recovery reason. Inspect available disk space and
   retained staging before raising this setting in `PythonDataService/.env` and
   recreating the data service; repeat the capture trigger afterward.
5. Evening observations are reference data. Compare a week's observations under
   `lake/raw/usa_options/<underlying>/observations/` to investigate rollover.
   Open interest is never asserted to be that session's settled figure.

## Recover a lost lake

1. Stop the data service, research runtime and every reader or writer of this
   lake. Do not start them again before step 6. Preserve any surviving
   root/catalog pair unchanged for diagnosis.
2. Select a generation containing a `COMPLETE` marker under the destination's
   `generations/`. Keep its sibling `objects/` folder: generations share objects.
   Do not copy a generation by itself or use a dot-prefixed staging directory.
3. Use the release/migrations recorded in `manifest.json` (the versions in
   the target's `schema_migrations` table, ADR 0081; a manifest written before
   that change lists the retired EF migration ids and does not restore into it).
   Provision an empty target catalog table with that exact schema and fresh
   empty target roots.
   Supply the target database credential through `POSTGRES_URL`; never paste it
   into a command or a recovery report.
4. From `PythonDataService/` in a checkout, run `python -m
   scripts.restore_lake_generation GENERATION --root
   ROOT_UUID=/empty/target/base-root`. Add `--root` for each root in the manifest.
   Without a checkout, use the copies each generation carries: `manifest.json`'s
   `restore_files` names the `objects/` entries holding `restore_lake_generation.py`
   and this runbook. Extract both, then run `python restore_lake_generation.py`
   with the same arguments; it needs only Python and asyncpg, both in the data
   service image.
5. The tool verifies the complete marker, dump, schema, migrations, files and
   root markers before writing anything, then imports the binary catalog dump
   and the exact file bytes together. It clears all leases, fences old lease
   generations and marks unfinished fetches failed. It starts no jobs, never
   reindexes, rebases no hashes and makes no vendor call. A failed restore leaves
   `.restore-incomplete.json` in each target root: discard that target and
   repeat into another empty pair.
6. No check decodes a recovered pair before it serves. The weekly restore check
   decodes the newest backed-up generation into temporary targets through the
   running data service, so it neither runs while the stack is stopped nor reads
   this pair. Point the stack at the recovered root/catalog and start it with
   provider access blocked and `OPTION_ARCHIVE_ENABLED=false`. Inspect artifact
   counts, normal Options Backtest saved timelines and retained Golden
   Search/Golden Options proofs. Restore provider access and re-enable the
   archive only after they match.

## A damaged backup generation

Pruning never removes a generation that fails verification and never counts it
as a good one; it logs `backup_generation_unverified` with the generation id, and
the archive status reports the backup at risk, naming that generation.
Diagnose it, then delete that `generations/<id>` folder yourself. Until then, a
damaged generation whose `manifest.json` cannot be read stops the removal of
unreferenced objects.

## Review and retire superseded option files

Retirement is a separate, owner-approved operation (#3432). Stop every process
that reads or writes the selected lake before applying it. The root maintenance
lock also refuses an active reader or publisher, and the backup destination's
lock refuses an apply while a backup or pruning runs ("retry later"); do not
bypass either.

1. The restore point is the backup generation the archive's restore check last
   passed on: the `restore` receipt in `option-archive-state.json` at the base
   of the lake. It must contain the exact original contract ZIPs and catalog
   receipts, and the check must be under seven days old both when the plan is
   made and when it is applied. Plan soon after a weekly restore check, or
   trigger `/restore` first.
2. From `PythonDataService/`, run `python scripts/retire_option_files.py
   --raw-root /ABSOLUTE/BASE/lake/raw --backup-destination /ABSOLUTE/BACKUP
   --plan /ABSOLUTE/retirement.json`.
   Supply the catalog credential through `POSTGRES_URL`. This writes a dry-run
   plan, including the restore receipt, and its digest; it deletes nothing and
   refuses to overwrite a plan.
3. Post that exact plan on #3432. Verify saved-run pins, retained study inputs,
   approved Golden Options proofs and complete golden results through the normal
   readers with provider HTTP blocked. Record the tested revision. Obtain the
   owner's approval of this exact plan and execution moment in that issue.
4. Write an approval JSON matching `RetirementApproval` in
   `app/data_lake/option_retirement.py`: the plan digest, owner approval comment
   URL, approval time in UTC milliseconds, compatibility revision, and the four
   successful compatibility checks. Repeat the command with `--apply
   --approval /ABSOLUTE/approval.json --journal /ABSOLUTE/retirement-journal.json`.
   Keep both the approval and journal. Every source and replacement is rechecked
   under root exclusion before unlinking and deleting its catalog row.
5. The plan can be applied for seven days. Its restore generation is pinned for
   seven days past that, so every deletion keeps a full rollback interval.
   Repeat an interrupted apply with the same plan, approval and journal: only
   that journal lets a retry accept a source the interrupted apply already
   unlinked, and a fresh journal refuses it. A changed source or replacement
   requires a fresh plan and fresh approval. Roll back into an empty
   root/catalog pair using the procedure above; never refetch irreplaceable
   history from the vendor.

Clerk stores, money trails and retained proofs are outside the deletion scope.
Partial sessions remain partial, and original per-contract byte pins continue
reading the exact ZIP member retained in the session container.

## Measure the frozen two-year benchmark

The two cases selected in #3433 contain 480 canonical SPY sessions: a two-leg
weekly ATM setup and an eight-leg covered setup with stops and re-entry. Export
all parameters and case digests with `python scripts/benchmark_options_archive.py
cases --output /ABSOLUTE/cases.json` from `PythonDataService/`.

Run only after all 480 sessions are fully archived and the reviewed services
are deployed on the unchanged 8-CPU / 16-GiB research profile. Keep Options width
at one until #3340 qualifies another width. Supply `DATA_PLANE_CONTROL_SECRET`
through the environment. Run `python scripts/benchmark_options_archive.py run
--api-url http://API:PORT --runtime-url http://RUNTIME:PORT
--operational-url http://API:PORT/health --output /ABSOLUTE/measurements.jsonl`.
Use the actual private service URLs and an operational read route that is safe
to sample. The harness refuses missing archive coverage, changed input inventory,
another resource profile or an unqualified width before accepting measurements.

It performs five cold and five warm decoded-cache runs per case. A cold run
clears each worker's whole decoded cache, stock entries included, once. The OS
page cache is uncontrolled; memory, staging, connection and service-latency
peaks are observed samples at one-second intervals. The decoded-cache peak is
sized from fixed per-bar, per-contract and per-session estimates, so it is not
comparable with figures measured another way. Staging bytes include each lock
file's one-byte sentinel. For each separately qualified width,
run against that deployed width and pass the same `--qualified-widths` set to
measurement and reporting. Do not change resource caps to obtain a passing time.
Generate the report with `python scripts/benchmark_options_archive.py report
--observations /ABSOLUTE/measurements.jsonl --output /ABSOLUTE/report.json` and
attach the report and receipts to #3433. A report needs the complete case/width/
cache-mode matrix, exact results, phase timings and distinct saved runs. It
reports a miss when the typical median exceeds 60 seconds. These measurements
never qualify combined production capacity; #3340 owns the concurrent-load and
operational-headroom decision.
