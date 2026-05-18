# Phase 1b progress (2026-05-17, same PR)


After Phase 1a landed, the LEAN image pull completed; Phase 1b added:

- (b) **Image digest pinned.** `sha256:97884667be20077925996ac22b5e3e16e3a47e7363e01795151459d16786247c` (see top of doc). `PINNED_LEAN_IMAGE_DIGEST` in `app/lean_sidecar/config.py` is the source of truth; `scripts/lean_sidecar_pin_image.py` writes it from `podman image inspect`.
- (c) **Windows topology — provisional.** Launcher is a host Python process invoking podman over the WSL2/podman-machine VM. Workspace bind-mounted via the standard WSL2 path translation. UID/GID matching for `--user` is a Phase 1c fast-follow (see security matrix below). The "launcher in its own container with only the Podman socket mounted" hardening pass remains deferred.
- (d) **Security-flag viability matrix (Phase 1b run on the pinned digest):**

  | Flag                                       | Podman-startup | LEAN-runtime  | Status                          |
  |--------------------------------------------|----------------|---------------|---------------------------------|
  | `--cap-drop=ALL`                           | ✅ accepted    | ✅ ran clean  | **Mandatory** in `runner.py`    |
  | `--pids-limit=512`                         | ✅ accepted    | ✅ ran clean  | **Mandatory** in `runner.py`    |
  | `--read-only`                              | ✅ accepted    | ✅ ran clean  | **Mandatory** (Phase 1c — see note below) |
  | `--user=<dynamic>`                         | ✅ accepted    | ✅ ran clean  | **Mandatory** (Phase 1c — dynamic UID, see note below) |
  | `--tmpfs /tmp:rw,noexec,nosuid,size=256m`  | ✅ accepted    | ➖ untested   | Opt-in (caller passes flag)     |

  Phase 1b initially deferred `--read-only` and `--user`; Phase 1c
  promoted both to mandatory after the trusted-sample E2E proved them
  viable at full LEAN runtime. `--read-only` works because Phase 1c's
  `object-store-root` config override moved LEAN's ObjectStore out of
  the image overlay (`/Lean/Launcher/bin/Debug/storage`) into
  `/lean-run/output/storage` — a workspace-writable path under the
  single bind mount. `--user` resolves dynamically via
  `runner._container_user_spec()`: on Linux the container's UID/GID
  matches the launcher's `os.getuid()`/`os.getgid()` so the container
  can write to launcher-created workspace files (without the dynamic
  match, native Linux hosts hit POSIX permission errors on
  `workspace/output` writes); on Windows + WSL2 where `os.getuid`
  doesn't exist the helper returns `10001:10001` as a non-root
  fallback that works because the WSL2 mount layer doesn't enforce
  host POSIX ownership inside the container.

- (f) **Metadata staging from image.** Added `stage_lean_metadata_from_image(workspace, image_digest)` in `app/lean_sidecar/staging.py`. Uses `podman create` + `podman cp` (no run, no network) to extract `/Lean/Data/market-hours/market-hours-database.json` and `/Lean/Data/symbol-properties/symbol-properties-database.csv` into the workspace's `data/` subtree. The launcher then mounts only the workspace; LEAN reads the metadata from a hashable path under the audit boundary instead of from the image-baked defaults.
- (g) **End-to-end trusted-sample run.** Three tests in `tests/lean_sidecar/test_runner_e2e.py`:
  - `test_buy_and_hold_runs_clean` — baseline shape, **passes**.
  - `test_buy_and_hold_runs_with_cap_drop_all` — adds `--cap-drop=ALL`, **passes** at full LEAN runtime.
  - `test_buy_and_hold_runs_with_read_only_root` — adds `--read-only + tmpfs /tmp`, **xfails** with the ObjectStore message captured in the test docstring.
- (i, partial) **Bar-consumption audit file.** The trusted sample writes `observations.csv` to LEAN's `ObjectStore` recording `(ms_utc, close)` for every received bar; the Phase 2 parser will read this and assert non-zero consumption + the three-window alignment.

Two trusted-sample fixes also landed in Phase 1b:

- LEAN's launcher reads `config.json` from its working directory by default (which is the image-baked default config pointing at `BasicTemplateFrameworkAlgorithm`). The runner now always appends `--config /lean-run/project/config.json` as the launcher arg so the workspace config wins; this is the safety floor noted in `runner.py:CONTAINER_LEAN_CONFIG_PATH`.
- `bar.EndTime` arrives as a naive Python `datetime` in algorithm timezone (ET), not a wrapped .NET `DateTime`; the sample now attaches ET via `zoneinfo` before converting to int64 ms UTC.
- `SetBenchmark(lambda dt: 100)` pins a constant benchmark so LEAN's post-run `ResultsAnalyzer` does not try to read SPY daily data that the trusted-sample-window does not stage.
