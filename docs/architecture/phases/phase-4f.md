# Phase 4f progress (2026-05-17, follow-up PR — lean_error_categories on rehydrated runs)

Phase 4d/4e click-to-rehydrate populated the run panel but always
synthesized empty ``lean_errors`` buckets. A run that exited 0 with
classified LEAN errors (e.g., ``failed_data_requests``) showed the
correct red badge thanks to the Phase 5a follow-up that parses
``is_clean=`` from manifest notes — but the operator couldn't see
WHICH category was hit. Phase 4f closes that gap.

- **No new endpoint.** The launcher service already writes
  ``f"lean_error_categories={sorted(response.lean_errors.keys())}"``
  to ``manifest.notes`` (Phase 1a). ``_safe_load_manifest_summary``
  parses it into a typed ``lean_error_categories: list[str]`` on
  ``RunSummaryModel``.
- **Whitelist parse**, not free-text. ``_parse_categories_note`` only
  keeps bucket names in
  ``{analysis_failed, failed_data_requests, runtime_error, other}``
  — a future LEAN bucket name lands in the manifest before the
  launcher learns about it; rendering arbitrary text into the sidebar
  would be an XSS-shaped risk.
- **Frontend**: ``loadRun()`` uses
  ``rehydratedLeanErrors(summary.lean_error_categories)`` to populate
  the synthesized ``TrustedRunResponse``'s ``lean_errors`` with one
  placeholder line per hit category. The existing ``errorRows()``
  template surfaces the category name; the placeholder explicitly
  says "line content not in manifest — fetch /runs/{id}/log for
  details" so the operator isn't misled.
- **What 4f does NOT do.** Does not fetch the LEAN log on click (~1
  MiB; click stays snappy). Operators who want raw lines use the
  existing ``GET /runs/{id}/log`` endpoint via the path link in the
  run panel.
- **Test surface**: 5 router tests + 2 component specs.
