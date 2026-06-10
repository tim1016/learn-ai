# Session handoff — 2026-06-09, LEAN-sidecar parity blockers

> **2026-06-09 addendum — empirical findings after attempting the fix.**
> Branch `lean-sidecar/applehv-dotnet-env-allowlist` lands the
> infrastructure (allow-list widening, paired-flag validator, memory
> bump, composite profile, tests, ADR) and the Issue 2A test re-point.
> **Issue 1's proposed env-flag fix does NOT actually unblock the
> wide-window SIGILL.** Mapped failure surface:
>
> | Window      | DOTNET_*=0 flags | Outcome                                                                             |
> |-------------|------------------|-------------------------------------------------------------------------------------|
> | 6-day (12 zips)   | NO         | ✓ exit 0, clean — known-good baseline confirmed                                     |
> | 6-day (12 zips)   | YES        | ✗ exit 132, `Python.Runtime.Py.GILState.Finalize` race in `GC.RunFinalizers` (the flags REGRESS a previously-clean window) |
> | 2-month (90 zips) | NO         | ✗ exit 132 @ 628ms, SIGILL during Composer assembly load                            |
> | 2-month (90 zips) | YES        | ✗ exit 132 @ 1114ms, SIGILL during Python.Runtime load (slightly later, still crash)|
>
> Also tested with no effect: `DOTNET_JitDisableSimdHWIntrinsic=1`,
> `DOTNET_EnableHWIntrinsic=0`, dropping `--read-only`, memory at 3072m.
> Confirmed `dotnet --info` runs cleanly in the same image, so the .NET
> runtime itself works on AppleHV — the SIGILL is specific to LEAN's
> assembly load path.
>
> The composite profile `HardeningProfile.WITH_TMPFS_256M_AND_APPLEHV_DOTNET_FIX`
> is defined but **not wired into `lean_sidecar_service.py` by default**
> (would otherwise regress the 6-day window). A follow-up session that
> finds the right env values should re-add it via:
>
>     hardening_profile=HardeningProfile.WITH_TMPFS_256M_AND_APPLEHV_DOTNET_FIX.value,
>
> on the `LaunchRequest` in `app/services/lean_sidecar_service.py`.
>
> **Next-debugging-step shortlist for the SIGILL:**
> 1. Enable `DOTNET_DbgEnableMiniDump=1 DOTNET_DbgMiniDumpType=4` and
>    extract a coredump to identify the trapping instruction
>    (needs a sandbox relaxation: `--cap-add=SYS_PTRACE` +
>    writable `/tmp` for the dump). Add a new diagnostics-only profile
>    for this; do NOT widen the general allow-list.
> 2. Rebuild `learn-ai/lean-sandbox` Dockerfile derivative with
>    crossgen2 R2R disabled at build time, instead of trying to
>    bypass R2R at runtime.
> 3. Bump the upstream `quantconnect/lean` base image; the pinned
>    `sha256:4934c22c` is from before the SDK 10.0.x SVE/SME issue
>    was documented.
>
> Findings here are also mirrored into the ADR
> (`docs/architecture/lean-sidecar-lab.md` § "Container execution
> boundary").


**Purpose:** capture two issues uncovered while attempting a Python ↔ LEAN
EMA-crossover parity demonstration on local macOS Apple Silicon. Both are
real, both are blockers, neither was fixed in this session.

## TL;DR

1. **LEAN sidecar SIGILLs (exit 132) on any window staging more than ~12
   trade/quote zips.** Reproducible. The fix template is already in the
   repo — Backend uses it — it just hasn't been propagated to
   `lean-sandbox`.
2. **The committed cross-engine parity fixtures and the LEAN-Lab default
   trusted template produce zero trades on every window short enough to
   run.** "Parity" is currently a 0 == 0 tautology. The 63-trade
   bit-exact comparison only exists in
   `app/engine/tests/test_spy_validation.py`, which points at a remote
   sandbox path that doesn't resolve locally.

Pick (1) up first — it unblocks every other path.

## Evidence

### Issue 1 — SIGILL on wider windows

Three runs in this session, same image
(`sha256:e2186f2e3e3e2c1ffb579c8cdbd4f74211a9c453893cb8273685555031b8187e`),
same launcher pin, same `EMA_CROSSOVER_SOURCE` template, only the
date window varies:

| Run id | Window | Staged zips | Exit | Result |
|---|---|---|---|---|
| `engine_lab_spy_mq7bipqi` | 2026-06-02 → 06-09 | 12 | 0 | clean, 3,901 data points, 0 orders |
| `spy_retry_6day_1053832` | 2026-06-02 → 06-09 (retry) | 12 | 0 | clean, identical |
| `spy_ema_24q2_1053781` | 2024-03-28 → 05-31 | 91 | **132 SIGILL** | log tail dies right after Composer assembly load |
| `engine_lab_spy_mq7bz7nl` | 2025-12-11 → 2026-06-09 | 247 | **132 SIGILL** | same failure point |

All four crash logs end at the exact same Composer / Python.Runtime
load step — no `Engine.Main(): LEAN ALGORITHMIC TRADING ENGINE …`
banner, no JIT progress past assembly load. That signature matches the
.NET 10 R2R-baked SVE/SME mismatch documented in `compose.yaml` for the
Backend service:

> csc (Roslyn) SIGILLs with exit 132 on Apple Silicon under Podman
> applehv. cpuinfo advertises sve2/sme2/sme2p1, but the AppleHV-
> virtualized CPU can't actually execute every SVE/SME sequence the
> .NET 10 SDK's R2R-precompiled images contain.

Backend's documented fix:

- `DOTNET_ReadyToRun=0` — force JIT instead of R2R-baked native code
- `DOTNET_TieredCompilation=0` — skip tier-0 quick-JIT
- Memory ≥ 3 GiB — at 1 GiB csc still SIGILLs **even with both flags
  set**; the `compose.yaml` comment is explicit that this floor must be
  paired with the flags or the "fix" silently regresses.

The 6-day window passing is consistent with the resource-floor angle:
12 zips, small workspace, low JIT footprint — no R2R-SME-baked
function is exercised before the algorithm finishes. Larger workspaces
push enough .NET surface area through assembly load to hit the bad
intrinsic.

#### Why the fix isn't trivial to drop in

`PythonDataService/app/lean_sidecar/runner.py` deliberately makes the
sandbox argv immutable:

- `ALLOWED_HARDENING_TOKENS` is a frozenset whitelisting only
  `--tmpfs` and its two known specs. `--env` is not on it.
- `DEFAULT_RUN_LIMITS.memory_mb = 2048` lives in
  `app/lean_sidecar/config.py`.
- `_validate_hardening_flags` rejects unknown tokens by design; this
  was the Phase 1c security floor that gated arbitrary-user-source
  acceptance, so adding tokens is a security-flag-matrix change, not a
  one-liner.

#### Proposed fix

PR-sized, mechanical, ~30 LoC + tests:

1. `runner.py`:
   - Extend `ALLOWED_HARDENING_TOKENS` with `--env`,
     `DOTNET_ReadyToRun=0`, `DOTNET_TieredCompilation=0`. Be
     **exact** — values, not patterns; the validator currently does
     exact-match.
   - Extend `_validate_hardening_flags` paired-arg handling so `--env
     KEY=VAL` is treated like `--tmpfs <spec>` (consume two tokens).
   - Add `HardeningProfile.WITH_APPLEHV_DOTNET_FIX` →
     `("--env", "DOTNET_ReadyToRun=0", "--env",
     "DOTNET_TieredCompilation=0")`.
   - Add a composite profile
     `HardeningProfile.WITH_TMPFS_256M_AND_APPLEHV_DOTNET_FIX` so the
     existing tmpfs callers can opt into both — or refactor to allow
     profile composition.

2. `config.py`:
   - Bump `DEFAULT_RUN_LIMITS.memory_mb` from 2048 → 3072. Match
     Backend's 3G floor. Document the Apple-Silicon reason inline.

3. `app/services/lean_sidecar_service.py` (where `LaunchRequest` is
   constructed): default the trusted-runs path to the new composite
   profile. Without this the change is dead code.

4. Tests:
   - `tests/test_lean_sidecar_runner.py` — extend the security-flag
     matrix to cover the new tokens (both validate-accept and
     argv-positioning regression).
   - One end-to-end test that launches a wider window (say 30 trading
     days) and asserts `exit_code == 0`. Without an Apple-Silicon CI
     runner this test is local-only; mark `@pytest.mark.local_arm64`.

5. ADR update: the Phase 1c sandbox-shape ADR enumerates the allowed
   flags. Add the `--env DOTNET_*` whitelist with rationale.

#### Caveat — security review needed

`--env` is a more powerful primitive than `--tmpfs`. Restricting the
allowed values (not the flag itself) is what keeps the sandbox honest.
The reviewer will want to confirm that pinning literal `KEY=VAL`
strings (not just `--env <anything>`) preserves the no-arbitrary-flag
property the Phase 1c security review signed off on.

### Issue 2 — pinned parity fixtures all show zero trades

`tests/fixtures/golden/cross-engine-studies/cells/` has W6mo
fixtures for SPY, QQQ, AAPL, TSLA. Every `reconciliation_pinned.json`
contains:

```json
{"status": "passed",
 "trade_summary": {"gating_divergent_count": 0, "passed": true},
 "lean_order_events": [],
 "engine_lab_order_events": []}
```

Cross-reconcile on this session's 6-day clean run reproduced the same
shape: `{lean_total_fills: 0, engine_total_fills: 0, matched_count: 0,
divergent_count: 0, passed: true}`.

This is what the strategy parameters dictate:

- 15-min EMA(5)/EMA(10) crossover
- Entry gate: `fresh_cross AND (ema5−ema10) ≥ 0.20 AND 50 ≤ RSI ≤ 70`
- Exit: 5 bars (75 min) after entry

On 6 months of SPY 15-min bars the triple gate doesn't fire at all.
The committed LEAN reference fixture (`spy_lean_trades.csv`, 63 trades)
covers a 2-year window — that's the only known window where the gates
actually fire in volume.

#### What this means for "parity"

The `passed: true` status on every committed cell is technically
correct but operationally hollow — both engines agree on producing
nothing. The cross-reconciler categorizes divergences; with zero
events on each side there's nothing to categorize. The parity *signal*
is at floor.

#### Two follow-ups, pick one

**A. Bit-exact 63-trade validation (preferred, lower effort once #1 is fixed):**

`PythonDataService/app/engine/tests/test_spy_validation.py` already
asserts trade-by-trade bit-exact equivalence over 2024-03-28 → 2026-03-27
against `spy_lean_trades.csv`. Two things stop it running locally:

- `LEAN_DATA_ROOT = Path("/sessions/ecstatic-hopeful-volta/mnt/Lean/Data")`
  — a remote-sandbox path. The compose mounts `../Lean/Data:/lean-data`
  but the test reads via host path, not the container. Either:
  - Re-point the test at a configurable env var
    (`LEAN_DATA_ROOT_ENV`, default `../Lean/Data`); or
  - Restructure so the test runs inside the polygon-data-service
    container where `/lean-data` exists.
- The test is named `run_validation` and isn't a pytest case yet — it's
  invoked via `python -m app.engine.tests.test_spy_validation`. Decide
  if it should become a `@pytest.mark.slow` test or stay as a manual
  validator.

**B. Wider-window cross-reconcile fixtures (needs #1 fixed first):**

Once SIGILL is unblocked, regenerate the W12mo and W24mo cells in
`parity_matrix/matrix.py` (the structure is already defined, just
unpinned). The 24-month cell on SPY should produce ≥50 trades on each
side — that's the real parity signal we currently lack.

## Files / paths the next session needs

| Path | What's there |
|---|---|
| `PythonDataService/app/lean_sidecar/runner.py` | The sandbox argv builder — Issue 1 fix lives here |
| `PythonDataService/app/lean_sidecar/config.py` | `DEFAULT_RUN_LIMITS` — bump memory_mb here |
| `PythonDataService/app/services/lean_sidecar_service.py` | Where `LaunchRequest` is assembled; needs the new profile selected |
| `compose.yaml` (backend block) | Source for the documented .NET R2R-SME root cause |
| `PythonDataService/artifacts/lean-sidecar/spy_retry_6day_1053832/` | A known-clean run, useful as a control |
| `PythonDataService/artifacts/lean-sidecar/spy_ema_24q2_1053781/` | A known SIGILL run, useful for crash-log diff |
| `PythonDataService/app/engine/tests/test_spy_validation.py` | The 63-trade bit-exact validator (Issue 2A) |
| `PythonDataService/app/engine/tests/fixtures/spy_lean_trades.csv` | The committed LEAN reference trade log |
| `tests/fixtures/golden/cross-engine-studies/cells/` | Pinned fixtures — all 0-vs-0 (Issue 2 evidence) |

## What I did NOT change

Nothing in this session was committed. The repo state at handoff time
is:

- `M Frontend/Dockerfile`
- `M package-lock.json`
- `?? PythonDataService/.launcher.log`
- `?? PythonDataService/.launcher.pid`

These predate the session. I created and then completed a TaskList for
the parity exercise; if the harness drops it across the boundary,
re-create with the issue list above as the source of truth.

## Suggested next-session opening move

1. Cut a branch `lean-sidecar/applehv-dotnet-env-allowlist`.
2. Make the `runner.py` + `config.py` + `lean_sidecar_service.py`
   change. Tests first.
3. Re-run the 2-month window:
   ```bash
   curl -X POST http://localhost:8000/api/lean-sidecar/trusted-runs \
     -H 'Content-Type: application/json' \
     -d '{"run_id":"applehv_fix_smoke","start_ms_utc":1711632600000,
          "end_ms_utc":1717421400000,"starting_cash":100000,
          "template":"ema_crossover","symbol":"SPY",
          "data_source":"polygon","bar_minutes":15,
          "session":"regular","adjustment":"raw"}'
   ```
   `exit_code: 0` is the gate.
4. Then run cross-reconcile and confirm non-zero `*_total_fills` with
   matching counts. **That's** the parity demo the user asked for at
   the start of this session.
