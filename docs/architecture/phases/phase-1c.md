# Phase 1c progress (2026-05-17, same PR — review-driven hardening)


After Phase 1b, reviewer feedback flagged three blockers for *claiming*
fidelity / reconciliation-readiness even from the spike. They land in
this same PR before merge:

- **Clean-run classification beyond exit code.** Exit-code 0 was lying — LEAN can crash `ResultsAnalyzer`, fail `SubscriptionDataSource` reads, or raise in `Algorithm.Initialize` while still exiting 0 (Phase 1b shipped that bug; Phase 1c catches it). New `app/lean_sidecar/result_classifier.py` parses `output/log.txt` after every run and buckets `ERROR::` lines into four stable categories: `analysis_failed`, `failed_data_requests`, `runtime_error`, `other`. `LaunchResponse` now carries `lean_errors: dict[category, list[str]]` and a top-level `is_clean: bool` that is True only when exit code is 0, the run did not time out, AND the classified-error dict is empty. Test surface: `tests/lean_sidecar/test_result_classifier.py` (9 cases including representative log shapes harvested from real Phase 1b runs).
- **`observations.csv` visibility (bar-consumption gate (i)).** The trusted sample writes through LEAN's `ObjectStore`, which previously rooted at `/Lean/Launcher/bin/Debug/storage` (image overlay) — invisible to the manifest and unwritable under `--read-only`. `LeanConfig` now sets `object-store-root` to `/lean-run/output/storage` (a workspace path), `Workspace.object_store_dir` exposes it, and the E2E test asserts the audit file lands there with a non-trivial body. Until this PR, "bar-consumption inspectable" was a Phase 1 claim the code did not actually deliver.
- **Explicit handling of failed data requests.** LEAN's default minute subscription also requests quote bars; the post-run `ResultsAnalyzer` needs SPY daily for benchmark equity-curve; `InterestRateProvider` needs `data/alternative/interest-rate/usa/interest-rate.csv`; `LocalDiskMapFileProvider` warns when `map_files/` is missing. Phase 1c addresses them by:
  * staging a synthetic daily SPY bar per trading day (`stage_daily_bars`),
  * extending `stage_lean_metadata_from_image` to extract the bundled `/Lean/Data/alternative/interest-rate/` subtree alongside `market-hours` + `symbol-properties`,
  * creating empty `factor_files/` + `map_files/` directories (`stage_empty_corporate_action_dirs`) — the trusted-sample window has no corporate actions so empty is the right semantic.
  After Phase 1c, the only LEAN log noise the trusted sample emits is `_quote.zip` not found (LEAN's minute subscription requests Trade *and* Quote bars; staging quotes is Phase 5+ work). The E2E test asserts that *no* category other than this documented known-noise pattern appears.

Three smaller hardening items also landed:

- **Hardening flags allow-list with structural validation.** The `runner.ALLOWED_HARDENING_TOKENS` allow-list now rejects unknown tokens AND verifies that paired flags (e.g., `--tmpfs <spec>`) have a value token after the flag name. `extra_image_args` is removed from `LaunchRequest` entirely; callers cannot tack on post-image flags.
- **Post-run `workspace_max_mb` enforcement.** The launcher walks the workspace after `execute()` and raises `LaunchRejectedError("workspace_max_mb_exceeded", …)` if the cap was overrun. Symlinks are not followed. Live mid-run monitoring is the Phase 1c+ ADR item.
- **`launcher.log` operator-friendly form.** The plan header now writes a shell-quoted single-line form (`# shell: podman run --rm …`) in addition to the argv-per-line audit form.

### Trusted-sample reconciliation status

The trusted sample is **not reconciliation-grade**, by construction:

- `SetBenchmark(lambda dt: 100)` pins a constant benchmark so the post-run `ResultsAnalyzer` does not need market-cap benchmark data the sample does not stage.
- Brokerage / fill / commission models are LEAN defaults.
- Only five trading days of synthetic minute bars; no factor or map files; no quote bars.

Reconciliation-grade samples (Phase 5) will:

- Stage real benchmark daily data and remove the `SetBenchmark` hack.
- Pin Interactive Brokers brokerage and the documented fill / fee models per ADR §"Brokerage, fill, and fee policy".
- Stage factor / map files for any window that touches a corporate action.
- Stage quote bars for any algorithm that consumes quotes.

This boundary is captured in `buy_and_hold.py`'s docstring; the E2E test calls `_assert_trusted_sample_run` not `_assert_reconciliation_grade_run` (the latter is a Phase 5 fixture).

Open from this PR, queued for Phase 1d / Phase 5:

- `--user <uid>` and `--read-only` were promoted to mandatory sandbox flags in Phase 1c (PR #254). Workspace UID/GID matching on Windows + WSL2 is handled by the launcher; the read-only root + tmpfs combination is fixed in the sandbox profile.
- **Determinism gate** — re-run + byte-identical normalized-artifact comparison. Trivial to add now that the clean-run contract is enforced; deferred so this PR does not grow further.
- ~~Quote-bar staging~~ — landed in Phase 5c (synthetic zero-spread minute quotes alongside trade zips; eliminates the ``Cannot find file: ...quote.zip`` known-noise category).
- Real factor/map files for the reconciliation-grade Phase 5 fixtures (not for the spike).
- ~~Populate `bars_consumed_by_symbol` in the manifest writer~~ — landed in Phase 5e (per-symbol count from `observations.csv` line count; closes the bar-consumption half of invariant #16).
- ~~Populate `staged_data_window_ms` in the manifest writer~~ — landed in Phase 5d (envelope = first staged ET-midnight → last staged ET-midnight + 1 day, expressed as int64 ms UTC; DST-stable).
- ~~Hardening-profile enum to replace caller-supplied `hardening_flags` argv tokens~~ — landed in PR #261 (`HardeningProfile.MINIMAL` / `WITH_TMPFS_256M` / `WITH_TMPFS_64M`; back-compat — raw tokens still accepted).
