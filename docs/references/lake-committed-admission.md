# Committed lake-file admission (#2456)

## Failure and authority

At baseline `eb13d31a678e618ee2da270737a940099a8b6631`, publication promotes
staged files while holding the artifact row lock, then commits its completion
receipt. A crash or failed commit leaves a canonical file and a non-complete
catalog row. Filesystem-only readers previously accepted it.

This implements ADR 0049's catalog-verified integrity contract; it introduces
no mathematical formula, engine, vendor port, or tolerance change.

The admission check is `app/data_lake/admission.py::read_committed_bytes`.

## Recovery

A vanished writer cannot release its lease. Once that lease expires, the next
capture uses the existing fenced retry path to replace the orphan and commit
its receipt. The orphan remains inadmissible until that commit. No unfenced
cleanup deletes a path that a concurrent publisher might now own. A lost
catalog still follows ADR 0049's wipe-and-re-backfill recovery policy.

Strategy Lab's live Polygon LEAN path now makes one canonical preparation
attempt for missing derived quotes, obsolete adjustment receipts, or
uncommitted inputs, then repeats the full preflight. Preparation runs outside
the adjusted run's capture lock. Quotes are synthesized from admitted trade
bars; no historical-quotes subscription is required. Frozen Both-mode inputs
are never refreshed by this recovery path, and their snapshot and admitted
minute reads run together on a worker thread.

For metadata published before adjustment scope was stored on the catalog row,
the verified image bundle may fill a NULL scope on the exact complete row it
selected by the mode-bound data-contract hash. Root, path, content hash, and
byte count must still match; a different non-NULL scope or uncommitted row is
refused. Readers themselves never infer or relax the missing scope.

Lake-backed LEAN runs also retain an admitted copy of their interest-rate
file in the private workspace so the existing native-statistics verifier can
use the run's input after execution. The engine continues to read its
read-only lake mount. The Strategy Lab repair receipt (2026-09-27; in Git history)
holds the reproduction and UI evidence.
