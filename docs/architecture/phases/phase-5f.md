# Phase 5f progress (2026-05-18, follow-up PR — determinism gate + zip mtime fix)

ADR invariant #15 says reconciliation fixtures require a determinism
proof: same inputs → equivalent artifacts. Phase 5f ships the gating
test AND fixes the real bug it discovered on first run.

- **Bug discovered.** Every staged ``.zip`` file (trade, quote, daily)
  had a different SHA256 across two same-input runs. Cause:
  ``zipfile.ZipFile.writestr`` writes the entry's mtime as wall-clock
  ``now()`` by default, so two runs at different timestamps produce
  byte-different archives for the same content. ``staged_data.bar_zips``
  hashes in the manifest were therefore not stable.
- **Fix** in ``app/engine/data/lean_format.py``: new
  ``_write_deterministic_csv_zip`` helper that pins
  ``ZipInfo.date_time`` to the ZIP epoch (1980-01-01) and explicitly
  sets ``compress_type=ZIP_DEFLATED`` on the entry (so an implicit-
  default drift on a Python upgrade can't silently break the gate).
  All three call sites — minute trade zip, minute quote zip
  (Phase 5c), daily zip (Phase 1a) — refactored to use it.
- **Gate test** (``tests/lean_sidecar/test_determinism_gate.py``)
  posts ``/api/lean-sidecar/trusted-runs`` twice with identical
  inputs (different ``run_id``s), then asserts:
  * manifest equality outside the timing-derived ``_ALLOWED_TO_DIFFER``
    set (``run_id``, ``started_at_ms``, ``finished_at_ms``, ``notes``)
  * normalized result byte-identical via ``json.dumps(sort_keys=True)``
  * load-bearing per-field assertions on ``staged_data.bar_zips``,
    ``algorithm_source_sha256``, ``config_json_sha256``,
    ``bars_consumed_by_symbol`` — each with a friendly failure
    message pointing at the broken invariant.
- **Gated on ``requires_lean_image``** so CI skips it; humans run it
  after ``podman pull docker.io/quantconnect/lean:<digest>``. Locally
  with the image pulled: ~25s wall-clock for two runs + comparison.
- **What this does NOT do.** Doesn't reconcile against an external
  reference (Phase 5g — LEAN-Lab-vs-Engine-Lab — is a separate
  scope per mission-critical D3). Doesn't normalize timestamps in
  ``effective_algorithm_window_ms`` if LEAN's ResultsAnalyzer ever
  starts producing wall-clock-flavored values — the current
  allow-list is conservative and surfaces any future drift.
- **Test surface** — 1 E2E test (gated), 275 lean_sidecar tests
  pass with the image pulled, 1 skip otherwise.
