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
   Use the same absolute path in this marker and the setting in step 2.
2. Set `OPTION_ARCHIVE_BACKUP_HOST_DESTINATION` to that absolute host path and
   `OPTION_ARCHIVE_ENABLED=true` in the data service's environment. Compose
   mounts it at `/lake-backup`. Compose also declares the absolute host
   checkout and lake paths for containment checks across bind-mount aliases. Both `OPTION_ARCHIVE_BACKUP_DESTINATION` and
   `OPTION_ARCHIVE_BACKUP_MOUNT` must name that same container path.
3. The named defaults are capture at 01:00 ET, backup at 05:00 ET, and an evening
   observation 30 minutes after the calendar close. Half days follow the calendar.
   The first capture is the full still-accessible two-year backfill. Provider
   requests pause before the next regular open and yield to foreground option
   requests and the evening observation. The interactive history floor is unchanged.
4. Read `GET /api/option-archive/status` or the Option archive card on Data Lab.
   `POST /api/option-archive/trigger/capture`, `/snapshot`, `/backup`, and `/restore`
   require the normal data-plane control-secret header. They return acceptance;
   read the resulting receipt on the status route. Repeating a running trigger
   joins that job. The first-night throughput is the difference in answered
   contract counts divided by the pass's elapsed milliseconds, using durable
   session receipts in `option-archive-state.json` beside the lake.
5. An exhausted transient capture can be retried explicitly with authenticated
   `POST /api/option-archive/capture-session` carrying `underlying`, the canonical
   `session_ms`, and `retry_exhausted:true`. This scopes recovery to one session.
   Authentication or entitlement failures block that underlying across passes
   and nights. Correct provider access first, then explicitly recover the
   blocked session with `retry_exhausted:true`; only its successful complete
   capture clears the block. A transient recovery failure leaves it blocked.
   Full-chain capture shares `OPTION_ARCHIVE_STAGING_MAX_BYTES` (default 1 GiB)
   across foreground and scheduled collectors, including retained ticker progress,
   abandoned listing generations and session-publication temporary bytes. A full
   budget pauses the pass before another request and appears as `capacity_pause`
   with its recovery reason. Inspect available disk space and retained staging
   before raising this setting in Compose's environment and recreating the data service; repeat the capture
   trigger afterward. Keep verified progress intact. This pause does not spend a
   provider retry, stop independent backup, or authorize legacy-file retirement.
   The budget counts file-content bytes plus bounded bookkeeping headroom;
   filesystem allocation and directory overhead require additional free space.
   Ordinary stock and listing publications retain their existing staging policy.
6. Evening observations are reference data. Their JSON retains capture start/end,
   page receipts, field gaps, and only vendor timestamps on the corresponding
   nested metric. Compare a week's observations under
   `lake/raw/usa_options/<underlying>/observations/` to investigate rollover.
   Open interest is never asserted to be that session's settled figure.

The archive cutoff includes the two-year New York-date anniversary when it
is a trading session. A read-only probe observed at 1792557343508 served
270 bars for SPY241025C00580000 on session 1729517400000 (2024-10-21) and
refused earlier sessions. The observation was at 00:35:43.508 ET, close to
the default 01:00 ET capture window; it does not establish the vendor's
rollover clock at every wall-clock time. A vendor outside-data-plan response
remains authoritative evidence of a lost fetch opportunity. Interactive
composition keeps its existing margin and admitted history remains readable. Run
`scripts/probe_option_archive_boundary.py` with a known historical OCC ticker
and `--today-session-ms` to settle that day; it changes no lake or catalog bytes.

## Recover a lost lake

1. Stop the data service, research runtime and every reader or writer of this
   lake. Do not start them again until all checks below pass. Preserve any
   surviving root/catalog pair unchanged for diagnosis.
2. Select a generation containing a `COMPLETE` marker under the destination's
   `generations/`. Keep its sibling `objects/` folder: generations share objects.
   Do not copy a generation by itself or use a dot-prefixed staging directory.
3. Use the release/migrations recorded in `manifest.json`. Provision an empty
   target catalog table with that exact schema and fresh empty target roots.
   Supply the target database credential through `POSTGRES_URL`; never paste it
   into a command or a recovery report.
4. Run `PythonDataService/scripts/restore_lake_generation.py GENERATION --root
   ROOT_UUID=/empty/target/base-root`. Add `--root` for each root in the manifest.
   A copy of this self-contained script and this runbook is included in each
   generation's `restore_files`, addressed by SHA-256 in `objects/`, so the
   procedure survives losing the checkout. Python and asyncpg are in the data
   service image. Extract the two named objects before using them.
5. The entry point verifies the complete marker, dump, schema, files and root
   markers before writing anything. It imports the binary catalog dump and
   exact file bytes together, clears all leases, fences old generations and
   converts unfinished fetches to failed. It starts no jobs, never scans to
   reindex, rebases no hashes and makes no vendor call.
6. With provider access blocked, run the weekly restore check against this pair
   and inspect artifact counts and normal Options Backtest saved timelines and
   retained Golden Search/Golden Options proofs. Only then point the stopped
   stack at the recovered root/catalog and restart it. A failed restore leaves
   an incomplete target: discard that target and repeat into another empty pair.

## Automatic verification and retention

Every backup takes one repeatable-read snapshot of the whole catalog table,
including receipts and failed rows, and copies the whole stock/option lake,
root markers and sidecar coverage evidence. It compares committed file hashes,
fsyncs the dump and inventory, and publishes `COMPLETE` last. A changed source
causes a bounded retry; a failed attempt never advances backup freshness.

Backup runs independently of backfill. Defaults keep seven daily and four
weekly verified generations and never remove the last good one. Objects are
removed only when no retained generation uses them. Retirement pins in
`retention-pins/<generation_id>.json` bind the manifest hash and a rollback
`retain_until_ms`; a pinned generation and its objects survive normal pruning.

Once a week, a separate scheduler job restores the latest checked generation
into an empty temporary root and isolated PostgreSQL schema, admits and decodes
every committed artifact using the restored receipt inventory, records a
manifest-bound result, and removes only those temporary targets. This check
has its own freshness; a new backup does not make an old restore check green.
No source file is retired until its separate approval and compatibility gate
passes (#3432).


## Review and retire superseded option files

Retirement is a separate, owner-approved operation (#3432). Stop every process
that reads or writes the selected lake before applying it. The root maintenance
lock also refuses an active reader, publisher or backup; do not bypass it.

1. Select a completed backup generation containing the exact original contract
   ZIPs and catalog receipts. Complete a successful isolated restore test of
   that generation within the preceding seven days. Keep its receipt file.
2. From `PythonDataService/`, run `python scripts/retire_option_files.py
   --raw-root /ABSOLUTE/BASE/lake/raw --generation /BACKUP/generations/GENERATION
   --restore-receipt /ABSOLUTE/restore-receipt.json --plan /ABSOLUTE/retirement.json`.
   Supply the catalog credential through `POSTGRES_URL`. This writes a dry-run
   plan and its digest; it deletes nothing and refuses to overwrite a plan.
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
5. The plan expires after seven days. Its restore generation is pinned through
   that rollback deadline. Repeating an interrupted apply with the same plan,
   approval and journal is idempotent. A changed source or replacement requires
   a fresh plan and fresh approval. Roll back into an empty root/catalog pair
   using the procedure above; never refetch irreplaceable history from the vendor.

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

It performs five cold and five warm decoded-cache runs per case. The OS page
cache is uncontrolled; memory, staging, connection and service-latency peaks are
observed samples at one-second intervals. For each separately qualified width,
run against that deployed width and pass the same `--qualified-widths` set to
measurement and reporting. Do not change resource caps to obtain a passing time.
Generate the report with `python scripts/benchmark_options_archive.py report
--observations /ABSOLUTE/measurements.jsonl --output /ABSOLUTE/report.json` and
attach the report and receipts to #3433. A report needs the complete case/width/
cache-mode matrix, exact results, phase timings and distinct saved runs. It
reports a miss when the typical median exceeds 60 seconds. These measurements
never qualify combined production capacity; #3340 owns the concurrent-load and
operational-headroom decision.
