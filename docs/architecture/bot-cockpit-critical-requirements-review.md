# Bot Cockpit Critical Requirements For Review

**Date:** 2026-06-25
**Scope:** Production-critical implementation pass for PRD #689.

## Decisions Made

1. Activity repair uses a separate derived projection/cache under the instance
   artifact area. Activity reads never append to the authoritative
   per-instance broker-activity WAL.
2. Repaired fills reuse the backend broker-activity authoring logic, but receive
   stable synthetic projection identity and explicit `activity_repair_projection`
   provenance.
3. Closed-trade summaries from `trades.parquet` are separate
   `closed_trade_summary` Activity rows. They do not infer constituent fill
   ids until the backend has a reliable join key, and they are not counted as
   additional broker fills.
4. Broker evidence rows now use backend-authored trader labels such as
   "Broker positions refreshed" and "Broker executions refreshed". Unknown
   recorder calls route to an unmapped broker diagnostic.
5. Activity rows expose backend-owned `visible_row_id`, `fold_key`,
   `fold_count`, `cluster_key`, and `cluster_label`. Angular tracks projected
   event rows by `visible_row_id`.
6. Deploy account id is display-only and sourced from the connected broker
   session. Angular no longer sends `account_id` in deploy requests. The
   backend still tolerates legacy clients that send `account_id`, but treats it
   only as a consistency hint: mismatches are rejected and the daemon payload is
   authored from the connected broker account.
7. Bot name / `strategy_instance_id` is protected against accidental reuse
   across unrelated historical run ledgers. Exact idempotent redeploy of the
   same content-addressed run remains allowed, and same-instance recovery
   redeploys are allowed only when the request supplies a parent run whose
   ledger already belongs to that strategy instance.
8. Activity repair cache selection uses cheap artifact-existence and file-stat
   fingerprints before reading parquet. Warm cache hits do not scan
   `executions.parquet` or `trades.parquet`.
9. Closed-trade summaries derive symbol wording from trade/execution artifacts
   only. Request filter inputs do not affect cached projection content.

## Independent Review Findings Addressed

1. Backend account authority gap: fixed by adding a public deploy request model
   without authoritative `account_id`, deriving account identity at the
   data-plane boundary, rejecting mismatched legacy payloads, and adding router
   tests that bypass Angular.
2. Activity repair cache pre-scan: fixed by moving parquet reads behind cache
   miss/stale handling and adding a warm-cache regression test.
3. Activity repair cache symbol impurity: fixed by removing request-provided
   symbol from the cache build path.

## Review Required

1. Confirm that the Activity repair cache location is acceptable:
   `<artifacts>/live_instances/<strategy_instance_id>/activity_repair/`.
2. Confirm whether direct host-daemon `/deploy` should also be forbidden outside
   the data-plane API. The daemon still requires `account_id` because it does not
   own broker session state; it should be treated as a privileged host seam.
3. Confirm whether same-instance recovery redeploy with an explicit parent run
   should stay allowed. The implementation preserves it because it continues the
   existing evidence namespace rather than creating a new unrelated bot.
4. Confirm whether the first implementation of validated strategy packages may
   remain a follow-up slice. This pass did not add the package registry because
   the deploy form already has substantial validation and the production risk was
   concentrated in Activity visibility and account/identity safety.

## Not Finished In This Pass

1. Full validated strategy package registry and package hash drift enforcement.
2. Broad PrimeNG modernization beyond the narrow Activity table metadata badge
   styling.
3. Audit/Configuration page copy restructuring and deduplication.
4. Recent Incidents backend-authored folding.
5. Broker API Evidence contrast/copy polish outside Activity projection rows.
6. Full extraction of Activity projection composition out of the large
   `live_instances.py` router. This pass extracted display contract and repair
   cache logic, but the route still orchestrates WAL read, repair merge, and
   final projection assembly.

## Validation Run

Focused backend and frontend tests were run for the implemented critical paths.
Full project-scope test runs should still be used before merge because this pass
touched backend schemas, Angular contracts, deploy identity, and Activity
projection code.
