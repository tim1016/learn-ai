# Phase 5d progress (2026-05-17, follow-up PR — staged_data_window_ms in manifest)


Closes half of invariant #16. ``staged_data_window_ms`` was ``None``
in every manifest since Phase 1a, so reconciliation readers couldn't
diff requested-vs-staged windows to surface "we asked for N days, only
got M" cases. Phase 5d makes it the actual ET-midnight envelope of
every staged trading day.

- **New helper** — ``_staged_window_from_dates(dates)`` builds
  ``WindowMs(start_ms=first_date 00:00 ET, end_ms=(last_date + 1 day) 00:00 ET)``
  in int64 ms UTC. ET-midnight is the reference, not a fixed UTC
  offset — DST transition days are 23 or 25 ET-hours wide and the
  envelope reflects that exactly.
- **Empty list returns None** so the manifest's
  ``staged_data_window_ms`` stays unset rather than carrying a
  zero-length window that would falsely claim staging happened.
- **What this does NOT do.** Doesn't populate ``bars_consumed_by_symbol``
  (still ``{}``; the other half of invariant #16). That needs
  per-symbol bar-count extraction from observations.csv (Phase 5e
  candidate). Doesn't add gap detection inside the envelope —
  the staged window is the envelope, not the dense set of dates.
- **Test surface** — 10 unit tests (empty-list-None, single-day,
  multi-day, DST-spring-forward 23h envelope, DST-fall-back 25h
  envelope, sparse list uses first+last only, int64 ms typing,
  parametrized non-DST 24h-exact). 238 lean_sidecar tests pass.
