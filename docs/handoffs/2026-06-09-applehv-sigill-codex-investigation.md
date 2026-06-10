# Codex handoff — investigate LEAN-sidecar AppleHV SIGILL

**Date:** 2026-06-09
**Predecessor handoff:** `docs/handoffs/2026-06-09-lean-sidecar-applehv-sigill-and-parity-gates.md` (read the addendum, not just the original body)
**Branch where this work continues:** off `lean-sidecar/applehv-dotnet-env-allowlist` (PR #466)
**Target host:** Apple Silicon (M-series) macOS, podman 5.x on applehv
**Pinned LEAN image:** `localhost/learn-ai/lean-sandbox@sha256:e2186f2e3e3e2c1ffb579c8cdbd4f74211a9c453893cb8273685555031b8187e`
**Goal:** make the 2-month SPY EMA-crossover trusted-run return `exit_code: 0` on the trusted-runs endpoint without regressing the clean 6-day baseline.

---

## TL;DR

LEAN sidecar SIGILLs (exit 132) on Apple Silicon + podman applehv on any window staging more than ~12 trade/quote zips. The crash fires during managed assembly load (Composer → Python.Runtime), well before the algorithm runs. The previous session attempted Backend's csc fix (`DOTNET_ReadyToRun=0` + `DOTNET_TieredCompilation=0` + 3 GiB memory floor); it does **not** unblock LEAN's runtime SIGILL and actively introduces a separate GIL-finalizer race on the previously-clean 6-day window. The fix template is wrong; we don't yet know the right one.

Your job is to find the right fix or definitively rule out the easy paths. Three concrete approaches are listed below — pick the one with the best expected value, document what you find, and propose a one-line re-wire on the existing infrastructure.

---

## What's already done (do not redo)

PR #466 (`lean-sidecar/applehv-dotnet-env-allowlist`) has already landed the infrastructure:

- `PythonDataService/app/lean_sidecar/runner.py` — `ALLOWED_HARDENING_TOKENS` accepts `--env`, `DOTNET_ReadyToRun=0`, `DOTNET_TieredCompilation=0` as literal KEY=VAL tokens. Pair-structure validator consumes `--env KEY=VAL` as a two-token pair. New `HardeningProfile.WITH_TMPFS_256M_AND_APPLEHV_DOTNET_FIX` composes them with the existing `--tmpfs` spec.
- `PythonDataService/app/lean_sidecar/config.py` — `DEFAULT_RUN_LIMITS.memory_mb` is 3072.
- `PythonDataService/app/services/lean_sidecar_service.py` — `LaunchRequest` construction has a commented-out slot where `hardening_profile=HardeningProfile.WITH_TMPFS_256M_AND_APPLEHV_DOTNET_FIX.value` would go. **Intentionally not wired** because the flags regress the 6-day baseline.
- `docs/architecture/lean-sidecar-lab.md` § "Container execution boundary" documents the allow-list state.
- Tests in `PythonDataService/tests/lean_sidecar/test_runner.py` and `test_hardening_profile.py` cover the allow-list, pair validator, profile expansion, smuggle rejection, and Pydantic LaunchRequest round-trip.

**Do not** widen the allow-list further as a debugging shortcut. If you need a diagnostic-only flag (e.g. `--cap-add=SYS_PTRACE`, `DOTNET_DbgEnableMiniDump=1`), add a **new** `HardeningProfile.DIAGNOSTICS_*` enum value with its own scoped tokens, used only for the specific run that needs it, and gated behind a clearly named API parameter.

---

## Empirical findings already proven (do not re-bisect)

| Window | DOTNET_*=0 flags | Result | Crash signature |
|---|---|---|---|
| 6-day (12 zips) | NO | ✓ exit 0, clean | (baseline) |
| 6-day (12 zips) | YES | ✗ exit 132 | `Python.Runtime.Py.GILState.Finalize` race in `GC.RunFinalizers` at shutdown |
| 2-month (90 zips) | NO | ✗ exit 132 @ 628ms | SIGILL during Composer assembly load |
| 2-month (90 zips) | YES | ✗ exit 132 @ 1114ms | SIGILL during Python.Runtime load (slightly later, same outcome) |

Mechanically verified:

- `podman run --rm --entrypoint /bin/sh <image> -c 'env | grep DOTNET'` confirms the env vars reach the container's dotnet process when passed via `--env`.
- `podman run --rm --entrypoint /bin/sh <image> -c 'dotnet --info'` returns 0 with clean output. The .NET runtime itself works on AppleHV; the SIGILL is LEAN-assembly-specific.
- `--read-only` is not the trigger — removed it, still SIGILLs at the same point.
- `DOTNET_JitDisableSimdHWIntrinsic=1` and `DOTNET_EnableHWIntrinsic=0` were tried (alongside the ReadyToRun + TieredCompilation pair); no effect.
- Memory at 3072m is not the limiting factor — workspaces are ~4 MB; tested at 4096m too.

Backend (`.NET 10 + Hot Chocolate`, `compose.yaml` `backend:` block) uses `DOTNET_ReadyToRun=0 + DOTNET_TieredCompilation=0 + memory: 3G` to fix **csc** (Roslyn compiler) SIGILL at build time. That fix is real for csc, irrelevant for LEAN's runtime assembly load.

---

## Failure signatures (verbatim)

### Wide-window SIGILL (the actual blocker)

```
20260610 02:11:08.687 TRACE:: Using /lean-run/project/config.json as configuration file
20260610 02:11:08.803 TRACE:: Composer(): Loading Assemblies from /Lean/Launcher/bin/Debug/
20260610 02:11:08.946 TRACE:: Python for .NET Assembly: Python.Runtime, Version=2.0.54.0, Culture=neutral, PublicKeyToken=5000fea6cba702dd
<exit 132 here, no further output>
```

`launcher.log` for a wide-window run shows the full constructed argv plus `exit_code: 132`, `duration_ms: 1114` (with flags) or `~628` (without).

### GIL-finalizer race (caused by the DOTNET_* flags)

```
Engine shutdown...
Matplotlib created a temporary cache directory at /tmp/matplotlib-1y7ud_tm because the default path
   (/Lean/Launcher/bin/Debug/.config/matplotlib) is not a writable directory...
Unhandled exception. System.InvalidOperationException: GIL must always be released, and it must be
   released from the same thread that acquired it.
   at Python.Runtime.Py.GILState.Finalize()
   at System.GC.RunFinalizers()
```

The 6-day run completes its backtest cleanly and crashes during finalizer cleanup. Exit 132 here is .NET aborting after an unhandled managed exception, not a CPU instruction trap. Distinct cause from the wide-window SIGILL.

---

## Reproduce locally

Prerequisites:
- macOS Apple Silicon, podman 5.x on applehv, `podman compose up` running.
- LEAN launcher running on host: `cd PythonDataService && ./.venv/bin/python -m uvicorn app.lean_sidecar.launcher.app:app --host 0.0.0.0 --port 8090` (background it; pid lands in `PythonDataService/.launcher.pid`).
- `polygon-data-service` container restarted after any Python edit: `podman compose restart python-service`.
- Polygon API key set in `.env` for SPY data fetch.

Failing case (2-month window, the empirical SIGILL gate):

```bash
curl -X POST http://localhost:8000/api/lean-sidecar/trusted-runs \
  -H 'Content-Type: application/json' \
  -d '{"run_id":"codex_repro_2month","start_ms_utc":1711632600000,
       "end_ms_utc":1717421400000,"starting_cash":100000,
       "template":"ema_crossover","symbol":"SPY",
       "data_source":"polygon","bar_minutes":15,
       "session":"regular","adjustment":"raw"}'
```

Expected (today): `{"exit_code":132,"is_clean":false,...}`.

Inspect the run:
```bash
cat PythonDataService/artifacts/lean-sidecar/codex_repro_2month/workspace/launcher/launcher.log
ls  PythonDataService/artifacts/lean-sidecar/codex_repro_2month/workspace/data/equity/usa/minute/spy/  # ~90 zips
```

Known-clean baseline (don't regress this):

```bash
curl -X POST http://localhost:8000/api/lean-sidecar/trusted-runs \
  -H 'Content-Type: application/json' \
  -d '{"run_id":"codex_repro_6day","start_ms_utc":1780407000000,
       "end_ms_utc":1781011800000,"starting_cash":100000,
       "template":"ema_crossover","symbol":"SPY",
       "data_source":"polygon","bar_minutes":15,
       "session":"regular","adjustment":"raw"}'
```

Expected (today, on master + PR #466): `{"exit_code":0,"is_clean":true,...}`.

Bypass the launcher (faster iteration during instruction-level debug):

```bash
# Use the workspace from a prior run so all staging is already done.
WS=PythonDataService/artifacts/lean-sidecar/codex_repro_2month/workspace

podman run --rm --network=none --security-opt=no-new-privileges --cap-drop=ALL \
  --userns=keep-id --user=$(id -u):$(id -g) --cpus=2.0 --memory=3072m \
  --tmpfs /tmp:rw,noexec,nosuid,size=256m \
  -v "$PWD/$WS:/lean-run:rw" \
  localhost/learn-ai/lean-sandbox@sha256:e2186f2e3e3e2c1ffb579c8cdbd4f74211a9c453893cb8273685555031b8187e \
  --config /lean-run/project/config.json
echo "exit=$?"
```

---

## Three investigation paths — pick one, document the result

### Path A — Coredump-driven SIMD instruction trace (highest signal)

**Hypothesis:** the SIGILL is a specific SVE/SME (or other AArch64) instruction that the AppleHV-virtualized CPU advertises in cpuinfo but cannot execute. Identifying the instruction tells us exactly which `DOTNET_*` toggle (or which .NET source patch) avoids it.

**Subtasks:**

1. Add a new `HardeningProfile.DIAGNOSTICS_COREDUMP` to `runner.py` whose tokens are:
   - `--tmpfs /tmp:rw,noexec,nosuid,size=256m` (existing)
   - `--env DOTNET_DbgEnableMiniDump=1`
   - `--env DOTNET_DbgMiniDumpType=4` (heap dump)
   - `--env DOTNET_DbgMiniDumpName=/tmp/coredump.dmp`
   Add each literal `KEY=VAL` to `ALLOWED_HARDENING_TOKENS`. **Do not** add `--cap-add=*`; the runner's mandatory shape rejects it. If `SYS_PTRACE` turns out necessary, propose it as a separate ADR change, not part of this profile.
2. Add a new field to the trusted-runs API request (e.g. `diagnostic_profile: Optional[str]`) so a caller can opt into the diagnostics profile per-run. Default unset.
3. Wire `lean_sidecar_service.py` to forward `diagnostic_profile` into the `LaunchRequest.hardening_profile` slot when set.
4. Run the 2-month repro with `diagnostic_profile: "diagnostics_coredump"`. The minidump should land at `PythonDataService/artifacts/lean-sidecar/<run_id>/workspace/.../coredump.dmp` (depends on what podman exposes from container `/tmp`).
5. Extract the trapping PC and disassemble it:
   - `lldb -c <dump> -- /Lean/Launcher/bin/Debug/dotnet` (from inside the image to get the right symbols), or
   - `dotnet-dump analyze <dump>` if a working `dotnet-dump` is available for aarch64.
   Report: which DLL the trapping instruction belongs to, the instruction mnemonic, and the surrounding bytes.

**Success criteria:** you can point at a specific instruction (e.g. "`SMSTART SM` inside `Python.Runtime.dll`'s R2R'd init") and propose either (a) a different `DOTNET_*` toggle that disables that specific intrinsic path or (b) a rebuild that drops the R2R'd version of the offending DLL.

**If it fails:** the dump might not write (tmpfs+`--read-only` interaction), or `dotnet-dump` may not run on the host without the matching SDK. Document the obstacle and fall through to Path B.

---

### Path B — Rebuild `learn-ai/lean-sandbox` with R2R disabled at build time

**Hypothesis:** if the trapping code lives in R2R-precompiled DLLs that ship with the LEAN base image, runtime `DOTNET_ReadyToRun=0` may be too late — the R2R native pages get mmapped during assembly load and trap before the JIT decides whether to use them. Rebuilding the image without R2R'd code entirely removes the trap surface.

**Subtasks:**

1. Inspect `PythonDataService/lean_sidecar/Dockerfile`. Today it's a thin derivative of `quantconnect/lean:latest` that only chmods `/root` to 0755. The base image already has the R2R'd DLLs baked in; there's no `dotnet publish` step we control.
2. Decide: do we rebuild LEAN from source (heavyweight — needs the QC repo + a multi-stage Dockerfile) or do we strip the R2R native sections from the existing DLLs post-load (lighter but fragile)?
   - **Source rebuild path:** add a stage that clones `https://github.com/QuantConnect/Lean` at a pinned commit, runs `dotnet publish -c Release -p:PublishReadyToRun=false -p:TieredCompilation=false`, copies the output over the base image's `/Lean/Launcher/bin/Debug/`. Pin the QC commit in `docs/references/lean-engine.md`.
   - **Strip path:** use `corerun --diagnostics` or `crossgen2 /Op` (unR2R) on each DLL in `/Lean/Launcher/bin/Debug/` as a Dockerfile RUN step. Only feasible if those tools accept already-published binaries.
3. Rebuild the image:
   ```bash
   podman build -t localhost/learn-ai/lean-sandbox:applehv-r2r-off PythonDataService/lean_sidecar/
   python PythonDataService/scripts/lean_sidecar_pin_image.py --tag applehv-r2r-off
   ```
   Update `ALLOWED_IMAGE_DIGESTS` and `PINNED_LEAN_IMAGE_DIGEST` in `app/lean_sidecar/config.py` to the new digest.
4. Re-run the 2-month repro. Pass criterion: `exit_code: 0` and `lean_total_fills > 0` on a window where the original image SIGILLs.

**Success criteria:** the new image runs the 2-month window clean, and the 6-day window stays clean. Open a PR that updates `Dockerfile`, the pinned digest, `docs/references/lean-engine.md` (note the QC commit), and the ADR.

**If it fails:** if the source rebuild also SIGILLs, the trap isn't in R2R-baked code — it's in either the JIT itself or in `libpython3.11.so` (loaded via P/Invoke from `Python.Runtime`). Move to Path A or escalate.

---

### Path C — Upstream `quantconnect/lean` image bump (cheap probe)

**Hypothesis:** the .NET 10.0.x SVE/SME issue may be already-fixed in a newer upstream LEAN image. The pinned base `quantconnect/lean@sha256:4934c22c…` was selected before the issue was documented.

**Subtasks:**

1. Pull the latest `docker.io/quantconnect/lean:latest`, inspect its labels (`target_framework`, `lean_version`).
2. Rebuild our derivative (`PythonDataService/lean_sidecar/Dockerfile`) on top of the new base. Update `ALLOWED_IMAGE_DIGESTS` + `PINNED_LEAN_IMAGE_DIGEST`.
3. Re-run the 2-month repro. Pass criterion: same as Path B.

**Success criteria:** the new image runs the 2-month window clean; the 6-day stays clean; existing fixtures (`tests/lean_sidecar/`) still match (re-run `test_runner.py` + `test_hardening_profile.py` on the host where podman is on PATH).

**If it fails:** rule out the cheap fix. Bump effort to Path A or Path B.

**Caveat:** a base image bump may invalidate the existing trusted-template fixtures and the LEAN data-folder contract (zip layout, factor file format). If `test_buy_and_hold_runs_with_*` E2Es regress, the bump is not net-positive and the prior image must be re-pinned with a documented reason.

---

## Constraints

- **Do not regress the 6-day window.** It's the only known-clean baseline. Run `codex_repro_6day` after every change.
- **Do not relax the Phase 1c sandbox shape** (`--network=none`, `--cap-drop=ALL`, `--read-only`, `--user <non-root>`, workspace-only mount) outside of an explicitly named `HardeningProfile.DIAGNOSTICS_*` that is per-run-opt-in.
- **Do not widen `ALLOWED_HARDENING_TOKENS` with patterns.** Literal `KEY=VAL` only; new values are explicit ADR-tracked decisions.
- **Do not delete the existing `WITH_TMPFS_256M_AND_APPLEHV_DOTNET_FIX` profile** unless your fix supersedes it — leave the scaffolding so an alternate hypothesis can be tested without re-adding the plumbing.
- Work on a new branch off `lean-sidecar/applehv-dotnet-env-allowlist` (the PR #466 branch). Open a follow-up PR; do not push to master directly.
- Match the existing test conventions in `tests/lean_sidecar/`. Any new profile or API field needs a parametrized smuggle-rejection test analogous to `test_refuses_unpinned_env_value`.

---

## Acceptance gate

A fix is accepted when:

1. `codex_repro_2month` returns `exit_code: 0`, `is_clean: true`, `lean_total_fills > 0`.
2. `codex_repro_6day` returns `exit_code: 0`, `is_clean: true` (no regression).
3. Project-scope ruff and the runner+hardening test suites pass (modulo the pre-existing 12 "podman not on PATH" baseline failures that affect tests run inside the data-plane container).
4. The fix is documented in `docs/architecture/lean-sidecar-lab.md` § "Container execution boundary" and an addendum to this handoff with the empirical evidence.

---

## Open questions worth answering even if no fix lands

- Which DLL's R2R section is the trapping instruction in? Composer (`QuantConnect.Composer.dll`), Python.Runtime, something they load next?
- Is the trap in managed JIT'd code or in `libpython3.11.so` (called via P/Invoke from `Python.Runtime`)?
- Does the same image run cleanly on a different Apple Silicon host (M1 vs M3, different macOS version, different podman version)? If yes, the trap is sensitive to a specific microarchitectural feature flag exposed to AppleHV.
- Is `quantconnect/lean` available as an `linux/amd64` image we could run under Rosetta as a fallback? Slower but might unblock CI.

---

## Files and paths the next investigator needs

| Path | Purpose |
|---|---|
| `PythonDataService/app/lean_sidecar/runner.py` | `ALLOWED_HARDENING_TOKENS`, `HardeningProfile` enum, pair validator, `build_command` |
| `PythonDataService/app/lean_sidecar/config.py` | `DEFAULT_RUN_LIMITS`, `PINNED_LEAN_IMAGE_DIGEST`, `ALLOWED_IMAGE_DIGESTS` |
| `PythonDataService/app/lean_sidecar/launcher/models.py` | `LaunchRequest` Pydantic model; new API fields land here |
| `PythonDataService/app/lean_sidecar/launcher/service.py` | Routes `hardening_profile` into `build_command` |
| `PythonDataService/app/services/lean_sidecar_service.py` | Where the trusted-runs path assembles `LaunchRequest`; the empty `hardening_profile=...` slot lives at the marked comment |
| `PythonDataService/lean_sidecar/Dockerfile` | The derivative image; Path B's edits go here |
| `PythonDataService/artifacts/lean-sidecar/<run_id>/workspace/launcher/launcher.log` | Per-run constructed argv + exit code + log tail |
| `PythonDataService/artifacts/lean-sidecar/<run_id>/workspace/output/log.txt` | LEAN's own log; absent when SIGILL fires before flush |
| `compose.yaml` (backend block) | Source of the documented (and inapplicable to LEAN) Backend csc fix |
| `docs/architecture/lean-sidecar-lab.md` | The ADR; § "Container execution boundary" |
| `docs/handoffs/2026-06-09-lean-sidecar-applehv-sigill-and-parity-gates.md` | Predecessor handoff with the original failure table |
