# Phase 5e progress (2026-05-17, follow-up PR — bars_consumed_by_symbol in manifest)

Closes the other half of invariant #16. ``bars_consumed_by_symbol``
was ``{}`` in every manifest since Phase 1a, so an auditor couldn't
tell whether the algorithm actually consumed bars — just that the
exit code was 0. Phase 5e populates it from the trusted sample's
already-written ``observations.csv`` audit file.

- **New helper** — ``_count_bars_consumed(workspace, symbol)``. Reads
  ``<workspace>/output/storage/observations.csv``, returns
  ``{<symbol.upper()>: <data_row_count>}`` or ``{}`` when the file
  is missing / empty / header-only / unreadable. The empty-dict
  branch handles every "no evidence" case identically so downstream
  consumers don't need to distinguish.
- **No silent excepts.** ``OSError`` on read logs a WARNING with
  the workspace path before returning ``{}``, per
  ``.claude/rules/numerical-rigor.md`` — never swallow an exception
  without context.
- **Symbol uppercased** to match the rest of the staging layer's
  canonical convention. A ``symbol="spy"`` request still keys
  bars under ``"SPY"``, no duplicate entries.
- **What this does NOT do.** Doesn't extract per-symbol counts for
  multi-symbol algorithms — observations.csv is single-symbol by
  trusted-sample convention. A schema change to observations.csv
  is a Phase 5f+ candidate (deferred per mission-critical doc D1).
  Doesn't reconcile observed-vs-staged counts; that's a Phase 5g
  comparator's job.
- **Test surface** — 9 unit tests (missing/empty/header-only/single
  bar/multi-bar/trailing-blanks/uppercased-symbol/OSError-logs-warning/
  pure-read-no-create). 250 lean_sidecar tests pass.
