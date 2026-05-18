# Design handoff — P1.4 live workspace cap

**Use Claude (design) for this one** — it's a runtime-architecture
choice with three viable approaches, each with real tradeoffs. Don't
let a code-only agent autonomously pick.

## What's broken

`workspace_max_mb` is enforced **post-execute** in
`PythonDataService/app/lean_sidecar/launcher/service.py` (line ~144 —
after `execute()` returns, the launcher walks the workspace and
raises `LaunchRejectedError("workspace_max_mb_exceeded", ...)` if the
cap was overrun).

For arbitrary user source (Phase 4c onward — `algorithm_source` from
the request lets a caller paste any QCAlgorithm), this means an
algorithm can fill the entire workspace until either:
1. The wall-clock timeout fires (after the LEAN process has already
   filled disk and possibly consumed all free space on the host
   filesystem), or
2. The host disk genuinely exhausts (every other process suffers).

The ADR claims "monitor and kill on overrun" but the launcher
delivers post-fact enforcement only. Reviewer P1.4.

## Three viable approaches

### Approach A — background poller thread in the launcher

The launcher spawns a thread that polls workspace size every ~1s; on
overrun, it reads the cidfile (already in place after PR #280) and
runs `podman stop --time=5 <cid>` + `podman rm <cid>`. Result is
written into the same `RunResult` shape with
`workspace_max_mb_exceeded` as the rejection reason.

**Effort:** medium — thread lifecycle, atomic stop-flag, no shared
state on success paths.
**Tradeoff:** ~1-second overshoot window before the kill fires (fast-
write algo can briefly exceed before the poller notices); reliable
SIGTERM with a clean operator-facing reason; plays well with the
existing P1.1 cidfile.
**Recommended.**

### Approach B — `--storage-opt size=<MB>m` on the workspace mount

Add `--storage-opt size=<workspace_max_mb>m` to the `podman run`
argv (or similar via container storage driver). The kernel enforces
the cap at write time; LEAN gets `ENOSPC` and crashes when the cap
is hit.

**Effort:** small — argv addition + flag in the RunLimits + tests.
**Tradeoff:** requires the overlay storage driver (default on most
podman installs but not guaranteed); LEAN's `ENOSPC` crash is messy
(non-zero exit, partial log, possibly corrupted `result.json`); the
existing `result_classifier` would need a new diagnostic category
for "ENOSPC mid-run".

### Approach C — tmpfs-backed workspace with size cap

Mount the workspace via `--tmpfs <path>,size=<MB>m` instead of the
current bind mount. Kernel enforces the cap, and the workspace
disappears at container exit (durability moves to a separate
`--volume` for output).

**Effort:** medium — changes the mount type, breaks the Phase 1c
determinism gate (it hashes workspace contents post-run), needs a
re-architecture of where artifacts persist.
**Tradeoff:** strongest enforcement; biggest blast radius (the
trusted-sample artifacts contract and the run-history sidebar both
read from the workspace bind mount, so a tmpfs-only path needs
output to land elsewhere). Probably overkill for the threat.

## Recommended: Approach A

Background poller. Reasons:

- Plays cleanly with PR #280's cidfile (no new state to track).
- Sub-1s overshoot is acceptable; the existing post-execute check is
  a backstop for the rare case where the poller misses a final
  flush.
- Operator-facing reason stays `workspace_max_mb_exceeded` — same
  envelope as the post-fact rejection callers already handle.
- Doesn't touch the workspace mount, the determinism gate, or the
  result.json schema.

## Implementation pointers (when an agent picks this up)

Files most likely to touch:

- `PythonDataService/app/lean_sidecar/launcher/service.py` —
  `launch()` spawns + joins the poller thread around the `execute()`
  call; sets a `threading.Event` to stop the poller on normal exit;
  routes overrun → `_kill_container_via_cidfile` (already exists, PR
  #280).
- `PythonDataService/app/lean_sidecar/runner.py` — `_kill_container_via_cidfile`
  may need to surface a "killed-for-cap-overrun" signal vs the
  existing "killed-for-timeout" signal so the launcher can return
  the right rejection reason. Or pass an enum-style reason into the
  kill helper.
- `PythonDataService/app/lean_sidecar/config.py` —
  `_WORKSPACE_POLL_INTERVAL_S = 1.0` constant.
- `PythonDataService/tests/lean_sidecar/test_runner.py` — new test
  mocks `subprocess.run` to take 5s, writes to workspace mid-run,
  asserts the kill helper is invoked at the right moment.

Gotcha: walking a workspace directory tree (`Path.rglob`) every 1s
under a hot LEAN run is itself I/O. Cap the poll at every-2s if perf
is a concern; the overshoot window scales with the interval.

## Test surface to plan for

- Happy path: small write, poller never trips, normal exit.
- Overrun path: simulate a write that exceeds the cap mid-execute,
  assert the kill helper is called with the cidfile, assert the
  returned RunResult.rejection_reason names workspace_max_mb_exceeded.
- Race path: write that lands JUST as `execute()` exits — the
  post-execute check should still catch it (the poller is a
  best-effort first line, not the only enforcement).
- No regression on PR #280's timeout-kill behavior — both kill paths
  use the same cidfile + helper.

## What this PR does NOT need to do

- Don't add `--storage-opt size=`. That's Approach B and gets
  deferred unless A turns out to be insufficient.
- Don't change the manifest schema — the existing `failure_reason`
  note (from PR #279) already carries the rejection signal.
- Don't change the API request shape — `workspace_max_mb` remains
  in the request as today.

## Independence

This work has no upstream PR dependencies once #279 (failure manifest)
and #280 (cidfile + timeout-kill) land. It can be a single PR on its
own.
