# Phase 1a progress (2026-05-17)


Shipped in PR following Phase 0:

- (a) **Launcher service authored** — `PythonDataService/app/lean_sidecar/launcher/`. Pydantic request model enforces digest pin + run-id slug + limit positivity. The service writes the planned `podman run` argv to `workspace/launcher/launcher.log` *before* spawning so an audit trail survives a launcher crash. `LaunchRejectedError.reason` is a stable label (`"workspace_not_staged"`, `"runner_configuration_error"`, `"invalid_run_id_or_path"`) for caller-side routing without parsing free text.
- (a, cont.) **Workspace path-under-root contract** — `app/lean_sidecar/workspace.py`. `run_id` is a strict slug (`^[a-z0-9][a-z0-9_-]{2,63}$`); resolution rejects symlink escapes; layout creation is idempotent.
- (e) **LEAN data-folder fidelity proof** — `tests/lean_sidecar/test_data_folder_fidelity.py` (7 cases). Asserts deci-cent round-trip (the integer disk encoding is exactly `price * 10000`), ET timestamp normalization (UTC inputs serialize to the equivalent ET ms-since-midnight), canonical zip layout (`equity/usa/minute/<sym>/<YYYYMMDD>_trade.zip`), and the LEAN quantization floor at `0.0001` for the smallest representable price.
- **Manifest contract** — `app/lean_sidecar/manifest.py`. All `int64 ms UTC`; the serializer refuses `datetime` objects at the boundary; atomic temp+rename write; sorted-pretty JSON so the file hash is stable across Python dict-iteration changes.
- **Trusted Python sample** — `app/lean_sidecar/trusted_samples/buy_and_hold.py`. Class is `MyAlgorithm` (matches the ADR's documented default). `SetCash` is explicit, `fillForward=False`, `DataNormalizationMode.Raw` — the reconciliation-grade defaults from §"Fill-forward policy" and §"Data normalization mode policy" are wired in from the start so the sample is reconciliation-eligible without a future rewrite.
- **`config.json` authoring** — `app/lean_sidecar/lean_config.py`. Container-side paths hard-coded against the `/lean-run` mount; sorted-pretty JSON for stable hashing. Phase 1 confirms the exact key names against the pinned image (see Open Questions §5).
- **Test surface** — 59 unit tests passing; security-flag matrix + E2E sidecar test gated on the locally-pulled LEAN image (skip-with-clear-reason on hosts that have not pulled it).
