# Data lake adjustment dimension — design (issue #1839)

**Status:** approved-in-conversation (operator, 2026-08-28) — option A selected for the adjusted-bytes fork (see §4).

**Predecessors:**
- ADR 0049 — the data lake as market-data authority; files canonical, Postgres catalog/coordination only.
- `app/data_lake/path_policy.py::resolve_lake_root` docstring, which ledgers the `data_root_id` question to "the flag-flip slice (#1839)".
- `app/engine/data/policy_store.py::resolve_data_roots` docstring, which ledgers the adjusted story to "a later slice (#1839), not to this seam".
- `app/data_lake/cache_import.py` module docstring §"One lake root per adjustment mode" — the mechanism this design deletes.

## Goal

Let the lake serve adjusted bars, so `DATA_LAKE_ENABLED=true` stops 409-ing a default Strategy Lab backtest.

Acceptance claim:

> With the flag on, a backtest requesting `adjusted=True` resolves lake bytes instead of raising `LakeAdjustmentUnsupportedError`; raw and adjusted artifacts for the same `(market, symbol, trading_date, data_type)` coexist in one lake with disjoint on-disk paths; and no catalog row's `FilePath` changes.

## 1. What the investigation found

Two corrections to the initial sizing, verified against the live tree:

**The catalog is already ready.** `DataLakeArtifacts.PriceAdjustmentMode` is an identity column with a three-value CHECK (`raw`, `polygon_split_adjusted`, `lean_adjusted`, constraint `ck_price_adjustment_mode_enum`) and participates in the claim/coverage unique keys (`catalog_client.py` — every query already filters on it). `cache_import.py` already writes `polygon_split_adjusted` rows. **No migration is part of this work.**

**The collision is already handled — by locking the whole lake.** Because `resolve_lake_root()` has no adjustment dimension, a raw and an adjusted artifact for the same `(symbol, date)` would land on the same path. `cache_import` prevents that with a whole-root mutual exclusion: a `.lake_root_mode` marker file (`atomic.lake_root_mode_marker_path`), `check_lake_root_mode` / `commit_lake_root_mode` / `read_lake_root_mode`, `LakeRootModeConflictError`, an `--claim-unmarked-root-as` operator escape hatch, a `--lake-root` flag so each mode gets its own tree, and a cross-process advisory lock (`_LAKE_ROOT_CRITICAL_SECTION_LOCK`) holding check-then-commit atomic. Its own error text states the constraint: *"they would collide at the same on-disk path. Use a separate --lake-root per adjustment mode."*

So this issue is not "add adjusted support". It is **replace a whole-root mutual exclusion with a path segment** — restoring the pattern the pre-lake policy store already used (`policy_store.resolve_policy_root` → `<cache_root>/<source>-<raw|adjusted>`) and that the catalog already models.

## 2. The change

Give the lake root the dimension, **above** the LEAN tree:

```
<LEAN_DATA_WRITE_ROOT>/lake/<price_adjustment_mode>/equity/usa/minute/<symbol>/<date>_trade.zip
```

`resolve_lake_root()` becomes `resolve_lake_root(mode)`. `resolve_staging_root()` gains the same dimension so a staging→lake promote stays a same-filesystem rename within one mode subtree.

The placement above the LEAN tree is the load-bearing choice, and it is what makes this cheap:

- **Catalog `FilePath` is root-relative.** It is `LeanMinuteBarPath(...).relative_path()` and carries no root identity (`path_policy` module docstring says so explicitly). Adding a segment *above* `equity/` leaves every existing row byte-identical. Zero catalog data migration.
- **LEAN readers see an unchanged tree.** The reader is handed a data root and finds `equity/usa/...` directly inside it, exactly as today. No reader change, no LEAN-format change; only the sidecar *mount source* gains a segment.
- **The on-disk migration is one rename**: `lake/equity → lake/raw/equity`. Everything currently in the lake is raw by construction — `DataRunSpec.price_adjustment_mode` is pinned `Literal["raw"]` and the fetcher hardcodes `adjusted=false` — and where a marker exists it proves it.

### 2.1 Reference artifacts are duplicated per mode root, deliberately

Factor files, map files, and the market-hours / symbol-properties metadata are adjustment-independent: the same bytes belong in every mode root. Their DCH builders (`ensure_data._factor_file_dch`, `_map_file_dch`) already pass `price_adjustment_mode="raw"` as a placeholder for exactly this reason.

They are **copied into each mode root**, not shared. LEAN takes exactly one data root and must resolve factor/map/metadata inside it, so a shared location would require either symlinks (fragile across the container's read-only mount) or a layered reader — and layering was explicitly rejected when the lake was built, because a second root silently outranking the first would make the manifest fingerprint recorded on a run a lie (`resolve_data_roots` docstring). The duplicated bytes are CSVs and one JSON: kilobytes, against minute-bar zips that are the actual volume.

Hardlinking into each root was considered and rejected: it saves nothing that matters at this size and complicates the atomic-promote and immutability story.

The consequence in the catalog is two rows for one reference artifact, differing only in `PriceAdjustmentMode`, with identical `FilePath` and identical `file_sha256`. That is honest — the row asserts "this artifact exists in this root", the unique key includes the mode, and matching hashes are the correct observable for content that genuinely does not vary by mode.

## 3. What this deletes

Once two modes cannot name the same path, the marker machinery guards nothing. Removed entirely:

- `check_lake_root_mode`, `commit_lake_root_mode`, `read_lake_root_mode`, `lake_root_mode_marker_path`
- `LakeRootModeConflictError` and its `lake_root_mode_conflict` skip reason
- the `--claim-unmarked-root-as` operator escape hatch and the `--lake-root` per-mode flag
- `_LAKE_ROOT_CRITICAL_SECTION_LOCK` and the advisory-lock critical section around check-then-commit

This is a net deletion. The 409 disappears at its source rather than acquiring a better error message.

`lean_sidecar/lake_mount.py::LAKE_SUBDIR` — a deliberate duplicate of the lake-root derivation, kept in lockstep by `tests/lean_sidecar/test_lake_mount.py`, whose own comment says "the integration slice collapses both onto a single resolver and deletes the parity test with this comment" — is collapsed onto `resolve_lake_root` here rather than being taught about modes.

## 4. The adjusted-bytes fork — decided

Two ways to serve adjusted, both anticipated by the catalog enum:

- **A. `polygon_split_adjusted`** — fetch `adjusted=true` from Polygon; store a second physical copy.
- **B. `lean_adjusted`** — store raw only, derive adjustment at read time from factor files (LEAN's own model).

**Selected: A.** B is a new numerical port — applying factor files to bars is math that needs its own golden fixture and tolerance under `numerical-rigor.md`, and folding it into a plumbing change would bury a real equivalence question inside a refactor. A is pure plumbing with an existing parity reference: `cache_import`'s existing rows are already `polygon_split_adjusted`, and the pre-lake policy store served exactly these bytes, so pre-lake parity is directly testable. `lean_adjusted` stays a reserved, unimplemented enum value.

## 5. Slices

1. **Path dimension + marker deletion.** `resolve_lake_root(mode)` / `resolve_staging_root(mode)`; thread the mode through every consumer (`ensure_data` ×3, `cache_import`, `chart_bar_source`, `policy_store` ×2, `lake_mount`); rename the on-disk tree; delete §3. Behaviour is unchanged — everything is still raw.
2. **Unblock the seam.** `resolve_data_roots` stops raising and resolves the requested mode; `DataRunSpec.price_adjustment_mode` widens off `Literal["raw"]`; the mode threads through `run_materialization`.
3. **Fetch adjusted.** `polygon_fetcher` honours the mode instead of hardcoding `"adjusted": "false"`, and `_DCH_MINUTE_TRADE_PARAMS` stops pinning `{"adjusted": False}`.

## 6. Verification

- **Pre-lake parity, mode-for-mode.** The equivalence proof already run for raw, repeated for adjusted: lake bytes == policy-store bytes for the same request.
- **A window containing a real split**, proving the dimension separates bytes rather than writing two identical trees. SPY is unusable (no split since 2005); NVDA's 2024 10-for-1 is the candidate, with the exact date confirmed against the vendor before it is pinned.
- **A no-split window** where raw and adjusted agree, so a test can distinguish "correctly identical" from "silently sharing one path".
- **Disjoint-path regression**, replacing what the marker used to guarantee: two modes writing the same `(symbol, date)` produce different absolute paths and two complete catalog rows.
- **Phantom-coverage regression**, the failure mode `resolve_lake_root`'s docstring warns about: a writer using a different root than a reader.

## 7. Risks and open items

- **`DATA_LAKE_ENABLED=true` is live in the operator's `.env`**, so default Strategy Lab backtests 409 until slice 2 lands. Flip to `false` for the duration unless the lake is being exercised deliberately.
- **Disk.** Option A stores a second full copy of every adjusted symbol/day. Acceptable at current scale; if it stops being acceptable, that is the argument for B, not for sharing a root.
- **`lean_adjusted` remains unimplemented.** Anything that writes it would be caught by the CHECK constraint but has no producer; no code path should offer it as a choice.
