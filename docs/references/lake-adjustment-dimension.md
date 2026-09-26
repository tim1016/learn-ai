# Data lake adjustment dimension — verification receipt (#1839)

What was verified, against what, on what date — for the change that gave the
lake a root per `price_adjustment_mode`.

**Date:** 2026-08-28
**Design:** `docs/superpowers/specs/2026-08-28-lake-adjustment-dimension-design.md`
**Code:** `app/data_lake/path_policy.py::resolve_lake_root`, `ensure_data`, `polygon_fetcher`, `app/engine/data/policy_store.py::resolve_data_roots`

## 1. The 409 is gone

Before: with `DATA_LAKE_ENABLED=true`, a default Strategy Lab backtest — which
carries no explicit `data_policy` and therefore defaults to `adjusted=True` —
was refused at root resolution with `LakeAdjustmentUnsupportedError`, surfaced
as HTTP 409.

After, against the live containerized data plane with the flag on:

```http
POST /api/engine/backtest
{"strategy_name":"ema_crossover_signal","params":{"symbol":"SPY"},
 "from_date":"2025-06-02","to_date":"2025-06-06","auto_fetch":true}

HTTP 200   success=True   total_trades=1
lake_data_availability_hash=f13a5f788b82096e...
```

## 2. The on-disk migration preserved every artifact

`scripts/migrate_lake_to_mode_roots.py --apply` on the live volume:

| | before | after |
|---|---|---|
| `lake/equity/...` | 533 files | — |
| `lake/raw/equity/...` | — | 533 files |

Catalog rows were not touched and did not need to be: `FilePath` is
root-relative, and the mode segment sits above the LEAN tree.

## 3. The fetcher genuinely honours the mode — proven across a real split

A no-split window proves nothing here: identical bytes are also what an
*ignored* flag produces. SPY is unusable for this (no split since 2005), so the
check used **NVDA across its 10-for-1 split on 2024-06-10**, materializing the
same window twice — once with the legacy adjusted default, once with an
explicit `data_policy.adjusted=false`.

Minute-trade zip SHA-256 (first 16 hex chars), same `(symbol, trading_date)`,
the two roots:

| trading date | raw | polygon_split_adjusted | |
|---|---|---|---|
| 2024-06-05 | `c39a250dc629a190` | `8f3bbb5d411cab9f` | **differ** |
| 2024-06-07 | `30a73ab387267228` | `377d7c51a2251e1a` | **differ** |
| 2024-06-10 | `8f9e1c135a59a610` | `8f9e1c135a59a610` | identical |
| 2024-06-12 | `373ac7f156589597` | `373ac7f156589597` | identical |

This is exactly the expected back-adjustment semantics, and it is why the split
date is the discriminator: Polygon back-adjusts history *before* a split and
leaves the split date and everything after it alone. Pre-split days differing
proves the vendor flag reached the request; post-split days matching proves the
adjustment is not being applied indiscriminately.

Both roots held 7 artifacts for the window (6 minute zips + the derived daily
rollup) and neither disturbed the other — the coexistence the deleted
whole-root marker used to make impossible.

## 4. What this receipt does *not* cover

- **`lean_adjusted`** has no producer. `ensure_data._polygon_adjusted_flag`
  refuses it rather than silently fetching one of the other two under its name.
- **Numerical equivalence of adjusted bars against a reference** is not claimed.
  This change is plumbing: it routes the vendor's `adjusted=true` response into
  its own root. Whether Polygon's split adjustment matches LEAN's factor-file
  adjustment is a separate question, and it is the question `lean_adjusted`
  exists to answer if it is ever built.
- **Dividend adjustment.** Polygon's aggregate `adjusted` flag is split-only,
  which is why the mode is named `polygon_split_adjusted` rather than
  `polygon_adjusted`.


## 5. Corporate-action versions (#2454, 2026-09-26)

Owner policy: [#2432](https://github.com/tim1016/learn-ai/issues/2432#issuecomment-5825763183)
requires latest-known adjustments, one version per run, recorded on the result.
The `polygon_split_adjusted` mode remains split-only; a dividend revision
invalidates its capture provenance even when the provider's prices stay identical.
No adjustment formula or numerical tolerance changes in this fix.

`app/data_lake/adjustment_versions.py` defines the version as SHA-256 of a
canonical, sorted snapshot of the symbol's full split and dividend responses.
The snapshot records whether announced actions are effective, so an announced
split becoming effective also changes the version. Event dates are persisted
as integer UTC milliseconds using the lake's existing calendar-date anchor.
Current snapshots live in `adjustment_versions/<symbol>.json`; immutable
historical snapshots remain under `adjustment_versions/<symbol>/<version>.json`.
This is a content version of our observed provider responses, not a token
issued by the provider.

`ensure_data` reads actions before and after an adjusted capture. Its per-symbol
file lock serializes adjusted writers on the shared lake volume. Publishing a
new current version immediately invalidates older adjusted files. Requested
minute days and previously published minute days with stale receipts are fetched
on the current basis, including days whose earlier refresh failed. Daily rollups and synthetic quotes carry that same version;
a failed source rebuild withholds the daily rollup. If the action set changes
during capture, the attempt is refused and the newly discovered version
invalidates its files for the retry.

The existing `DataLakeArtifacts.CorpActionRevision` column now records the
version during lease-fenced publication. The recipe hash includes it. A sibling
`<artifact>.adjustment.json` binds the version to the archive's digest and is
published under the same lease as the archive. A missing, stale, or torn
companion is refused by readers and rebuilt by capture. Pre-version adjusted
caches therefore require a successful rebuild; raw caches retain their existing
recipes, bytes, and behavior.

Python readers validate the exact bytes they parse and pin one version per
symbol for their lifetime. A rebuild during a run cannot switch its basis.
Availability checks include version evidence in their cache key. Sweep
snapshots bind both archive and companion bytes, which also catches a dividend
revision that leaves archive bytes identical. LEAN's external reader holds the
same symbol lock from preflight through manifest creation, preventing adjusted
capture from changing its mounted basis while the container reads it.

Receipts expose `corporate_action_versions` on the lake availability response,
Python backtest response, persisted execution configuration, sweep snapshot,
and LEAN staged-data manifest. These versions identify the inputs actually
read, rather than re-reading the current version after execution.

Validation:

- `tests/engine/test_adjustment_versions.py`: the original capture-before-split /
  capture-after-split regression failed on master with `[100, 10]` and passes
  with `[10, 10]` for minute and daily readers. Also covers raw invariance,
  dividend-only changes, failed rebuilds, missing/torn records, concurrent
  captures, mid-capture/mid-run revisions, quote derivation, sweep pinning,
  saved run receipts, availability invalidation, retained action history, and
  recovery of previously published history after a failed refresh.
- `tests/unit/data_lake/test_ensure_data.py::test_split_refresh_commits_one_version_to_the_real_catalog`:
  repeats the split through real Postgres claim/refresh/publication and checks
  the persisted revisions and both readers.
- `tests/lean_sidecar/test_lake_mount_service.py`: verifies that the adjusted
  launch holds the basis lock, releases it afterwards, and saves its version.

The provider fetcher has no revision token for aggregate requests. Bracketing
with the action responses detects revisions observable through those endpoints;
it does not independently prove that the provider's aggregates and reference
endpoints are internally synchronized. The prior mode-level verification's
limits on vendor numerical equivalence still apply.
