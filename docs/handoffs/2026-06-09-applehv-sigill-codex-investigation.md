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

---

## Addendum — 2026-06-10 Codex investigation

### What changed in the diagnosis

The best current explanation is no longer "LEAN assembly R2R contains a bad SVE/SME intrinsic." It is more specific: the pinned LEAN image runs CoreCLR 10.0.2 on an AppleHV-exposed ARM64 CPU feature set that advertises SME/SME2 but not standalone SVE. dotnet/runtime issue `#122608` documents the same macOS Virtualization.Framework/M4/container failure shape, and dotnet/runtime PR `#127398` fixes it by removing a CoreCLR signal-context path that executed the SVE `rdvl` instruction when an SVE signal-frame record was present on SME-only hardware.

Relevant upstream evidence:

- `https://github.com/dotnet/runtime/issues/122608` — `.NET 10 SDK ARM64: Illegal instruction (SIGILL) on Apple M4 with macOS Virtualization.Framework`, closed 2026-04-28.
- `https://github.com/dotnet/runtime/pull/127398` — `Fix SIGILL crash on ARM64 platforms with SME but no SVE`, merged to `main` on 2026-04-28. The PR body says CoreCLR was calling `CONTEXT_GetSveLengthFromOS()`, which executes `rdvl`, from signal context handling; on Apple M4 + Virtualization.Framework the kernel may provide an SVE context record because of SME streaming SVE even though standalone SVE instructions are illegal.

Local facts from this host:

- Host: podman 5.8.2, rootless AppleHV machine, Rosetta enabled.
- Pinned derivative image: `localhost/learn-ai/lean-sandbox@sha256:e2186f2e3e3e2c1ffb579c8cdbd4f74211a9c453893cb8273685555031b8187e`.
- Pinned image architecture/runtime:
  - `uname -m`: `aarch64`
  - `.NET SDK`: `10.0.102`
  - `.NET host/runtime`: `10.0.2`
  - LEAN labels: `lean_version=17748`, `target_framework=net10.0`
  - `/proc/cpuinfo` includes `sme`, `sme2`, `sme2p1`; it does not list `sve`/`sve2`.
- Latest pulled `docker.io/quantconnect/lean:latest` arm64 image is still `.NET host 10.0.2` with LEAN label `lean_version=17764`, so a normal latest-base bump is unlikely to contain PR `#127398`.
- `quantconnect/lean:latest` is multi-arch. Manifest inspection shows an `amd64/linux` digest (`sha256:a21369b90a5b1da4c5f458da8e956f5c1f091c4e2ed5a2b5d5eac8f04cdd9bf5`) and an `arm64/linux` digest (`sha256:780427dc35095e6986a80d4ae68dee2593c33f5cd46184275923ca03b08e03ce`). A full amd64 pull was started as a probe but stopped because it was large and not needed for this diagnosis.

### Implications

1. `DOTNET_ReadyToRun=0`, `DOTNET_TieredCompilation=0`, `DOTNET_EnableHWIntrinsic=0`, and `DOTNET_JitDisableSimdHWIntrinsic=1` are probably the wrong fix class. The upstream bug is in CoreCLR signal-context capture, not in managed SIMD codegen or R2R payload selection. That matches the empirical result: the env flags did not unblock the 2-month run and introduced the separate Python.Runtime GIL-finalizer crash on the 6-day baseline.
2. Rebuilding LEAN assemblies with `PublishReadyToRun=false` may also be a lower-value path than it looked. If the trap is CoreCLR 10.0.2's `libcoreclr.so` signal handler, replacing LEAN DLLs will not remove it.
3. The highest-value fix path is to run LEAN on a .NET runtime that contains dotnet/runtime PR `#127398`, or avoid native arm64 CoreCLR under AppleHV by running the amd64 LEAN image via Rosetta.

### Concrete next experiments

Recommended order:

1. **Runtime-patch derivative image**: build a new `localhost/learn-ai/lean-sandbox:*` derivative that starts from the current pinned LEAN image but replaces `/root/.dotnet/shared/Microsoft.NETCore.App/10.0.2` and host/fxr components with the first .NET 10 servicing build that contains PR `#127398` once available. Before doing this, verify the servicing version/changelog or inspect `libcoreclr.so` symbols/source package so the image change is not guesswork. Then pin the new derivative digest and run both gates:
   - 2-month SPY EMA crossover: expect `exit_code=0`, `is_clean=true`, `lean_total_fills > 0`.
   - 6-day baseline: expect `exit_code=0`, `is_clean=true`.
2. **amd64/Rosetta fallback probe**: build the same thin derivative from `quantconnect/lean@sha256:a21369b90a5b1da4c5f458da8e956f5c1f091c4e2ed5a2b5d5eac8f04cdd9bf5` with `--platform linux/amd64`, add an explicit platform field to the runner or a separate pinned image repo name, and run the two gates. This is likely slower but avoids the ARM64 CoreCLR/AppleHV bug entirely and is supported by the upstream issue's reported workaround.
3. **Coredump only if the two image-level paths fail**: if a patched CoreCLR or amd64 image still SIGILLs, then add the `DIAGNOSTICS_COREDUMP` profile from Path A and capture the trapping PC. At that point the failure is likely not PR `#127398` and needs instruction-level evidence.

### Small cleanup noticed

`PythonDataService/tests/lean_sidecar/test_hardening_profile.py::TestLaunchRequestModel.test_hardening_profile_accepts_applehv_dotnet_fix_value` has a stale docstring saying `lean_sidecar_service.py now defaults trusted-runs to this profile`. The service intentionally does not wire that profile because it regresses the 6-day baseline. The test behavior is fine; the comment should be corrected in the follow-up PR.

---

## Addendum — 2026-06-09 amd64/Rosetta empirical bisection (rules out Path 2)

### TL;DR

Codex's Path 2 (amd64 LEAN via Rosetta as a workaround for the AArch64 SIGILL) is **empirically ruled out** on this host (podman 5.8.2 applehv rootless on Apple Silicon, Rosetta enabled, podman machine 16 CPU / 32 GiB / 100 GiB). The .NET 10 runtime itself runs cleanly under Rosetta, but LEAN's JIT'd code path consistently crashes Rosetta during startup. The bisection in this addendum should save the next investigator the ~30 minutes of pull/build/restart needed to reproduce.

Codex's Path 1 (replace CoreCLR with a servicing build containing dotnet/runtime PR #127398) and the broader **external x86_64 Linux** option (cloud VM, native amd64 hardware, Windows host) remain the only known paths to wide-window LEAN execution on Apple Silicon.

### What was tried, what landed in the repo

PR-quality plumbing for the amd64 path was authored and is **deliberately kept in place as opt-in** (`PINNED_LEAN_IMAGE_DIGEST_AMD64`, `DIGEST_PLATFORMS`, `_platform_for_digest`, `--platform=linux/amd64` argv derivation, `Dockerfile.amd64`). The default pin (`PINNED_LEAN_IMAGE_DIGEST`) was switched back to the arm64 derivative after the empirical findings below; the arm64 6-day baseline was re-verified as clean (`exit 0`, 4.7s) after the revert.

- `PythonDataService/lean_sidecar/Dockerfile.amd64` — pins `quantconnect/lean@sha256:a21369b9…` (lean_version 17764, net10.0). FROM stanza uses `--platform=linux/amd64`. Mirrors the arm64 derivative's `chmod 0755 /root` (no-op on the amd64 image whose `/root` already ships 0755, retained for build uniformity).
- `PythonDataService/app/lean_sidecar/config.py`:
  - `PINNED_LEAN_IMAGE_DIGEST_AMD64 = "sha256:bdb7c7aa3bd5f196905442706f9ebd6d22de08e21cf6ac5cc74b621690005a75"` (the locally-built amd64 derivative).
  - `PINNED_LEAN_IMAGE_DIGEST_ARM64` retains the prior arm64 pin.
  - `ALLOWED_IMAGE_DIGESTS` is the union of both.
  - `DIGEST_PLATFORMS: dict[str, str]` keys the amd64 digest to `"linux/amd64"` so callers that opt in get a deterministic `--platform` flag at `podman run` time.
- `PythonDataService/app/lean_sidecar/runner.py`:
  - `_platform_for_digest(image_digest)` reads `DIGEST_PLATFORMS`. Returns `None` for native-arch digests.
  - `build_command` appends `--platform=<value>` immediately after `--rm` when the lookup is non-`None`; existing native-arm64 callers (and all `DUMMY_DIGEST` tests) get the same argv they got before.

### Empirical findings on amd64/Rosetta

Host: podman 5.8.2, applehv rootless, Rosetta enabled (`podman machine inspect` → `"Rosetta": true`).

| Smoke | Image / cmd | Result |
|---|---|---|
| `--platform linux/amd64 alpine:3.20 uname -m` | tiny | ✓ `x86_64` (Rosetta translates) |
| `--platform linux/amd64 debian:bookworm-slim` + Microsoft's `dotnet-install.sh` (.NET 10.0.9) | tiny | ✓ `dotnet --info` clean, `linux-x64` RID, exit 0 |
| amd64 LEAN derivative, `dotnet --info` inside the image | full LEAN image | ✓ .NET SDK 10.0.101 / Host 10.0.1 / `Ubuntu 22.04 / linux-x64`, no SIGILL |
| amd64 LEAN derivative + staged 6-day SPY EMA workspace (`amd64_repro_6day`) via real trusted-runs path | full | ✗ `exit_code=1`, `duration_ms=74149`. LEAN booted (Composer ✓, Python.Runtime ✓, Engine.Main ✓), then **NRE creating `Newtonsoft.Json.Converters.StringEnumConverter`** inside `QuantConnect.Util.MarketHoursDatabaseJsonConverter.Create` → `MarketHoursDatabase.FromFile`. Workspace JSON sha matches the amd64 image's `/Lean/Data/market-hours/market-hours-database.json` byte-for-byte (sha1 `7a27a324…`), so the input is not the cause. |
| amd64 LEAN derivative, **built-in BasicTemplate CSharp algorithm, no workspace mount** | image-internal | ✗ `qemu: uncaught target signal 11 (Segmentation fault) - core dumped` after `TextSubscriptionDataSourceReader.SetCacheSize(): Setting cache size to 71582788 items`. LEAN booted to the same point as above before crashing. |
| Same BasicTemplate run + `DOTNET_ReadyToRun=0 DOTNET_TieredCompilation=0` | image-internal | ✗ same segfault, same point |
| Same BasicTemplate run + `DOTNET_gcServer=0 DOTNET_EnableHWIntrinsic=0 DOTNET_JitDisableSimdHWIntrinsic=1 DOTNET_ReadyToRun=0` | image-internal | ✗ segfault even earlier — immediately after `Composer.LoadPartsSafely` |

Earliest TRACE noise in every LEAN-amd64 run is identical:
```
TRACE:: Composer.LoadPartsSafely(/Lean/Launcher/bin/Debug/System.Private.ServiceModel.dll):
  Skipping FileLoadException: ... assembly's manifest definition does not match the
  assembly reference. (0x80131040)
```
This warning is present on arm64 too and is not the proximate cause — but the upstream `quantconnect/lean` amd64 build does ship at least one mismatched assembly reference.

Diagnostic notes:
- Image is genuinely amd64 (`podman image inspect` → `Architecture: amd64`), and the JIT confirms `x64` host architecture at startup. The `qemu: uncaught target signal 11` is Apple Rosetta's qemu-user component surfacing a SIGSEGV it could not translate / deliver to the guest; this is a Rosetta-translation crash, not a managed .NET fault.
- The staged-workspace path's managed NRE (`StringEnumConverter()` constructor → `NullReferenceException` inside `JsonTypeReflector.GetCreator`'s compiled `Expression.New(...)` delegate) is also consistent with Rosetta-emitted x86_64 misbehaving on a specific reflection-emit code path: the constructor itself takes no args and does nothing reachable that could legitimately NRE; the failure is the JIT'd / Rosetta-translated call site, not the constructor body.
- The two failures (Rosetta SIGSEGV in BasicTemplate; managed NRE in the staged path) happen at different stages because the staged path needs `MarketHoursDatabase` deserialized before any data-feed code runs, so the Newtonsoft path is exercised first; the BasicTemplate path bypasses that and reaches `TextSubscriptionDataSourceReader.SetCacheSize` before Rosetta hits a different bad-translation site.

### Implications

1. **Path 2 (amd64 + Rosetta) is not viable on this host.** The .NET runtime itself works under Rosetta, but LEAN's specific JIT'd / reflection-heavy startup code does not. No subset of `DOTNET_ReadyToRun`, `DOTNET_TieredCompilation`, `DOTNET_gcServer`, `DOTNET_EnableHWIntrinsic`, or `DOTNET_JitDisableSimdHWIntrinsic` moves the failure point in a useful direction.
2. **Path 1 (runtime-patch derivative) remains the only known path to wide-window LEAN on Apple Silicon**, and is gated on a `.NET 10.0.x` servicing build that contains dotnet/runtime PR #127398.
3. **External x86_64 Linux is now the only known path that does not require waiting on Microsoft**: a cloud VM, a native amd64 workstation, or Windows on x86_64 hardware. This also happens to be the closest reproduction of the Windows-validated SPY EMA-crossover bit-exact baseline at `app/engine/tests/fixtures/spy_lean_trades.csv` — the parity claim the Python engine is meant to be validated against.
4. **The arm64 narrow-window path is unaffected.** The 6-day baseline (`exit 0`, ~5s) remains the only clean LEAN execution surface on this host today. Anything that can be reconciled at ≤ 12-zip windows can be validated against Python here and now; Phase 5g.3 (the `cross_runner` → `cross_reconciler` HTTP endpoint, currently a 501 stub) is the unblocked next step for that scope.

### Recommended next experiments

Priority order, given today's evidence:

1. **Wire Phase 5g.3 `POST /api/lean-sidecar/runs/{id}/cross-reconcile` (currently a 501 stub) to the existing `cross_runner` + `cross_reconciler` modules.** Run the SAME 6-day SPY EMA-crossover window through both engines on the SAME staged data, classify divergences with the existing 8-category taxonomy, and write the report to `docs/references/reconciliations/`. This is the path most aligned with the user's stated goal ("make LEAN match Python on the same data") that is unblocked on this host today. It will not exercise wide-window strategy state, but it does close the methodology gap.
2. **External x86_64 Linux for wide-window LEAN.** Pick a substrate that runs the amd64 derivative natively (cloud VM is cheapest to validate; a colleague's x86_64 Linux box is fastest). Re-run the 2-month gate. If clean, capture the trade log and start a wide-window reconciliation against Python.
3. **Watch dotnet/runtime PR #127398 for a 10.0.x servicing release.** When it lands, build a Path-1 derivative against the patched runtime, re-pin, and the arm64 wide-window SIGILL gate is cleared on this host without external infrastructure. No periodic polling required — the dotnet/runtime issue tracker (`#122608`) will surface the release.

### What was NOT tried (and why)

- **Older `quantconnect/lean` amd64 tags.** A version-matched amd64 base for `lean_version=17748` is not retrievable from Docker Hub (the multi-arch index for `:latest` has rotated). Pulling older arbitrary tags would be ~13 GB per probe with no a-priori reason to expect a working substrate. Not net-positive for this investigation; documented for completeness.
- **Coredump-driven instruction trace (Codex Path 3).** Punted to follow-up because the SIGSEGV happens in Rosetta-emitted code, not in CoreCLR-emitted code — symbolizing the trapping PC requires Rosetta's internal symbols, which Apple does not ship. The `dotnet-dump` path would yield managed-only frames and is unlikely to identify the root cause. The user message attached to the segfault (`qemu: uncaught target signal 11`) is already the strongest classifier available.

### Small cleanup landed in this work

- `PythonDataService/tests/lean_sidecar/test_hardening_profile.py::TestLaunchRequestModel.test_hardening_profile_accepts_applehv_dotnet_fix_value` — docstring corrected to reflect that `lean_sidecar_service.py` intentionally does **not** wire the AppleHV-DOTNET-FIX profile (its env flags do not unblock the wide-window SIGILL and introduce a separate GIL-finalizer race on the 6-day baseline). Behavior unchanged.

---

## Addendum — 2026-06-10 long-window SPY EMA check

User request: run a longer-duration SPY EMA-crossover check (6 months or 1 year) to see whether LEAN and Python have matching trades.

### Six-month run

- Run ID: `spy_ema_6mo_20260610_cx1`
- Window: 2025-12-10 09:30 ET through exclusive end 2026-06-10 09:30 ET
- Template / strategy: LEAN `ema_crossover`; Python `SpyEmaCrossoverAlgorithm`
- Staged data: 124 trade zips and 124 quote zips
- LEAN result: `exit_code=132`, `is_clean=false`, `duration_ms=724`
- LEAN crash point:
  ```text
  TRACE:: Using /lean-run/project/config.json as configuration file
  TRACE:: Composer(): Loading Assemblies from /Lean/Launcher/bin/Debug/
  TRACE:: Python for .NET Assembly: Python.Runtime, Version=2.0.54.0, Culture=neutral, PublicKeyToken=5000fea6cba702dd
  ```
- Normalized LEAN result: absent (`normalized_path=null`)
- Cross-reconcile endpoint result: 404 `normalized_missing`
- Python-on-same-staged-data result: 44 order events

First Python events:

```text
2025-12-10T14:15:00Z Buy 145 @ 684.75 fee 1.00
2025-12-10T15:30:00Z Sell 145 @ 688.12 fee 1.00
2025-12-18T14:45:00Z Buy 148 @ 676.28 fee 1.00
2025-12-18T16:00:00Z Sell 148 @ 680.03 fee 1.00
```

### One-year run

- Run ID: `spy_ema_1yr_20260610_cx1`
- Window: 2025-06-10 09:30 ET through exclusive end 2026-06-10 09:30 ET
- Template / strategy: LEAN `ema_crossover`; Python `SpyEmaCrossoverAlgorithm`
- Staged data: 251 trade zips and 251 quote zips
- LEAN result: `exit_code=132`, `is_clean=false`, `duration_ms=892`
- LEAN crash point:
  ```text
  TRACE:: Using /lean-run/project/config.json as configuration file
  TRACE:: Composer(): Loading Assemblies from /Lean/Launcher/bin/Debug/
  TRACE:: Python for .NET Assembly: Python.Runtime, Version=2.0.54.0, Culture=neutral, PublicKeyToken=5000fea6cba702dd
  TRACE:: Composer.LoadPartsSafely(/Lean/Launcher/bin/Debug/System.Private.ServiceModel.dll): Skipping FileLoadException: ...
  ```
- Normalized LEAN result: absent (`normalized_path=null`)
- Cross-reconcile endpoint result: 404 `normalized_missing`
- Python-on-same-staged-data result: 70 order events

First and last Python events:

```text
2025-06-18T10:15:00-04:00 Buy 166 @ 600.08 fee 1.00
2025-06-18T11:30:00-04:00 Sell 166 @ 600.10 fee 1.00
2025-06-30T09:45:00-04:00 Buy 161 @ 616.39 fee 1.00
2025-06-30T11:00:00-04:00 Sell 161 @ 616.01 fee 1.00
...
2026-05-28T10:15:00-04:00 Buy 137 @ 753.06 fee 1.00
2026-05-28T11:30:00-04:00 Sell 137 @ 753.94 fee 1.00
2026-06-09T09:45:00-04:00 Buy 139 @ 745.3598 fee 1.00
2026-06-09T11:00:00-04:00 Sell 139 @ 737.75 fee 1.00
```

### Conclusion

The long-window data and Python strategy are meaningful: Python emits trades on both 6-month and 1-year windows when run against the exact staged LEAN workspace data. LEAN still crashes before producing `log.txt` or normalized order events, so there is no LEAN trade list to compare. The blocker remains the native arm64 AppleHV/CoreCLR SIGILL described above; this host cannot prove long-window trade parity until either a patched .NET runtime image is available or the same runs are executed on external native x86_64 Linux.

---

## Addendum — 2026-06-10 runtime-patched arm64 fix

The native arm64 path is now unblocked on this host. A thin derivative image keeps the pinned LEAN engine payload at `lean_version=17748` and installs .NET Host/Runtime 10.0.9 side-by-side with the image's original 10.0.2 runtime:

- Dockerfile: `PythonDataService/lean_sidecar/Dockerfile.arm64-dotnet109`
- Upstream LEAN base: `docker.io/quantconnect/lean@sha256:4934c22c2b080a688f25b571746603e01533c5e581499d8457e5624a132ba77b`
- Runtime source: `mcr.microsoft.com/dotnet/runtime@sha256:62b592e657ceebbfd24203430542232559dcb7b73e45cc3ebb48c7bba8c2e2f0`
- New pinned default digest: `localhost/learn-ai/lean-sandbox@sha256:0b8d4e381b63daaa4cebbea7af294cc5b140793a6fd13f8c9cfd63ef2a2fb24d`

The runtimeconfig for LEAN targets `Microsoft.NETCore.App` version `10.0.0`, so normal patch roll-forward selects the installed 10.0.9 runtime. The amd64/Rosetta digest remains allow-listed and platform-keyed as opt-in only; it is not the default.

### Acceptance runs

All runs below used the normal `POST /api/lean-sidecar/trusted-runs` path through the host launcher and the data-plane container.

| Run ID | Window | LEAN result | Data requests | Trade reconciliation |
|---|---:|---:|---:|---:|
| `spy_ema_6day_dotnet109_cx1` | 2026-06-02 09:30 ET to 2026-06-09 09:30 ET exclusive | `exit_code=0`, `is_clean=true`, 0 orders, end equity 100000.00 | 12/12 succeeded | not needed; no fills |
| `spy_ema_6mo_dotnet109_cx1` | 2025-12-10 09:30 ET to 2026-06-10 09:30 ET exclusive | `exit_code=0`, `is_clean=true`, 44 LEAN orders, end equity 101254.03, `OrderListHash 34dc2c99a2b3cc6f92130b7976e76e29` | 250/250 succeeded | passed: 44 LEAN fills, 44 Python fills, 44 matched, 0 divergences |
| `spy_ema_1yr_dotnet109_cx1` | 2025-06-10 09:30 ET to 2026-06-10 09:30 ET exclusive | `exit_code=0`, `is_clean=true`, 70 LEAN orders, end equity 103222.54, `OrderListHash b4ef267072771c561a86673d484765f8` | 504/504 succeeded | passed: 70 LEAN fills, 70 Python fills, 70 matched, 0 divergences |

### Conclusion

Path 1 is accepted: the crash was fixed by replacing CoreCLR 10.0.2 with a 10.0.9 runtime inside the native arm64 LEAN derivative. The DOTNET env-flag profile remains diagnostic scaffolding only and should not be enabled for trusted runs. Rosetta remains ruled out on this host.
