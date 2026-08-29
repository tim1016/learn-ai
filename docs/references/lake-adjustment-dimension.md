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
