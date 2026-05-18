# Phase 4d progress (2026-05-17, follow-up PR — run-history sidebar)


After Phase 4c made arbitrary user source first-class, the page still
forgot every run the moment the operator submitted the next one or
refreshed the browser. Phase 4d adds the missing read-side affordance:
a sidebar that lists past runs and lets the operator click one to
rehydrate it in the main panel. No persistence change: the index is
built by scanning the artifacts root on demand.

- **API** — `GET /api/lean-sidecar/runs` returns `RunIndexResponse {
  runs, cap, truncated }`. The scan reads each `<artifacts_root>/<run_id>/manifest.json`,
  extracts a compact `RunSummaryModel` (run_id, symbol, requested
  window, started/finished ms, exit_code, `algorithm_source_kind`,
  `exit_clean`), and sorts by `started_at_ms` desc. Capped at 200
  rows so a pathological artifacts root cannot balloon the response.
  Pure read — does not touch the launcher, does not require LEAN to
  be running. Half-written or non-JSON manifests are silently skipped
  so a crash mid-write does not break the listing.
- **Slug-pattern filter at the directory boundary.** The scan only
  enumerates directories whose names pass `RUN_ID_PATTERN`, so a
  stray out-of-band tar extract (`/artifacts/Not a Slug!/manifest.json`)
  never reaches the response — the sidebar is not a free file-browser.
- **UI** — new `LeanLabRunHistoryComponent` (presentational): takes
  `runs`, `selectedRunId`, `loading`, `truncated` as inputs and emits
  `runSelected: string`. Renders a colored status dot per row (green
  for `exit_clean=true`, red for `false`, grey for null/no manifest)
  plus a "custom" tag when `algorithm_source_kind="user_provided"`.
  Parent `LeanLabComponent` owns the run-list signal, refreshes it on
  init + after every successful submit, and handles click by calling
  `getNormalized()` and rehydrating the main panel. The form fields
  are intentionally NOT repopulated on click — keeping the form
  primed for the next submit is the lower-surprise behavior. Form
  rehydration from manifest is a Phase 4e candidate.
- **`exit_clean` is intentionally weaker than `is_clean`.** The
  manifest doesn't store `lean_errors` so the index can't reconstruct
  the full clean signal (exit==0 AND no LEAN errors AND not timed
  out). The sidebar uses `exit_clean` only for at-a-glance row color;
  clicking a row still rehydrates the normalized result, which is
  where the operator gets the real picture.
- **Test surface** — 8 new router tests (sort order, manifest-missing
  skip, corrupt-manifest skip, non-slug skip, summary-field
  extraction, exit-clean false branch, legacy-manifest unknown-kind,
  empty root). 7 new component specs for the standalone sidebar
  (empty state, row render, truncated banner, custom-tag rendering,
  click emits, click disabled while loading, aria-current on
  selected). 5 new specs on the parent component for the integration
  (init load, submit re-refresh, loadRun rehydration, loadRun 404
  surfaces error envelope, listRuns rejection survives gracefully).
- **What Phase 4d does NOT do** — does not introduce a database; the
  scan-on-demand approach is fine at 200 rows but will need an index
  cache + a real persistence layer for the Phase 6 multi-thousand-run
  case. Does not stream live progress for in-flight runs (the index
  only shows manifest-written runs, so an in-progress run appears at
  the top only after completion).
