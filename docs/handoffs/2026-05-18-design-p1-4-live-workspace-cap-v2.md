# Design handoff — P1.4 live workspace cap (v2)

**Replaces** `2026-05-18-design-p1-4-live-workspace-cap.md` (kept for
history). Same shipped approach as v1 (background poller). v2 makes
the threat model explicit and tightens three implementation choices
the original handed away.

## Threat model (load-bearing — read first)

This cap defends against **benign overrun**, not **adversarial input**.

The app has no caller authentication today. That fact would normally
argue for kernel-enforced storage quotas (the kernel is the only thing
the algorithm can't lie to). It doesn't, because the data plane is a
single-host research tool — network surface is
`host.containers.internal` and localhost — and the realistic failure
mode is "I wrote a buggy `QCAlgorithm` that fills my own disk", not
"a remote actor exfiltrates compute resources."

If auth is added later and `algorithm_source_kind=user_provided`
starts arriving from a genuinely untrusted caller, **escalate this cap
to kernel-enforced** (`--storage-opt size=<mb>m` on `podman run`;
podman storage driver is already `overlay`, though
`containers/storage.conf` would need `overlay.size=` set, which is
absent today). The post-execute check is **not a sufficient backstop
against an adversary** — they can wedge the host before the check
fires. The poller alone is theater under that threat model.

This boundary is the most important content of this handoff. Encode
it in the module docstring of whatever class hosts the poller so the
next agent doesn't re-derive it.

## What ships

Background poller thread in the launcher, same shape as v1 Approach A,
with three tightenings:

### 1. Walk efficiency

`Path.rglob` is the wrong tool. It instantiates a Python `Path` per
entry, which under a hot LEAN run (thousands of small writes/sec in
`cache/` and intermediate `result.json` fragments) makes the poller
itself an I/O contention source.

Use `os.scandir` recursively (cheap C-backed `stat`). If measurement
on a 100k-file workspace shows scandir-loop is still expensive,
fall back to `subprocess.run(["du", "-sb", workspace_path], ...)`.
Both are O(entries) but with much lower per-entry cost than `rglob`.
Target: poller iteration ≤50ms on a 100k-file workspace.

### 2. Poll interval is NOT configurable

1s is the right default. v1 raised "every-2s if perf is a concern" as
a tradeoff knob — don't expose it.

- The interval IS the overshoot budget. Putting it in `RunLimits`
  invites callers to widen the budget unsafely.
- Future tuning happens by improving the walk, not lengthening the
  interval.

Keep as a module-level constant `_WORKSPACE_POLL_INTERVAL_S = 1.0` in
`PythonDataService/app/lean_sidecar/config.py`.

### 3. Kill-reason discriminator

PR #280's `_kill_container_via_cidfile` currently surfaces a single
"killed" signal. The launcher needs to know **why** to return the
right rejection reason.

Pass an enum into the kill helper and back out:

```python
class KillReason(StrEnum):
    WALL_CLOCK_TIMEOUT = "wall_clock_timeout"
    WORKSPACE_MAX_MB_EXCEEDED = "workspace_max_mb_exceeded"
```

The kill helper logs it, includes it in any kill-event payload, and
the launcher routes it directly to `RunResult.rejection_reason`. No
string magic.

## Files to touch

- `PythonDataService/app/lean_sidecar/launcher/service.py` — poller
  thread lifecycle wraps the `execute()` call; `threading.Event` stops
  the poller on normal exit; overrun routes via
  `_kill_container_via_cidfile(reason=KillReason.WORKSPACE_MAX_MB_EXCEEDED)`.
- `PythonDataService/app/lean_sidecar/runner.py` —
  `_kill_container_via_cidfile` gains a `reason: KillReason` param;
  threads it into the result envelope.
- `PythonDataService/app/lean_sidecar/config.py` — module-level
  `_WORKSPACE_POLL_INTERVAL_S = 1.0`. NOT exposed in `RunLimits`.
- `PythonDataService/tests/lean_sidecar/test_runner.py` — new tests:
  - Happy path: small write, poller never trips, normal exit.
  - Overrun path: write exceeds cap mid-execute; kill helper called
    with `KillReason.WORKSPACE_MAX_MB_EXCEEDED`; `RunResult.rejection_reason`
    matches.
  - Race path: write lands JUST as `execute()` exits; post-execute
    backstop still catches it.
  - Walk-cost smoke: 10k-100k synthetic files; one poller iteration
    completes in ≤50ms (assertion with generous margin).
  - No regression on PR #280 timeout-kill path.

## What this PR does NOT do

- **No kernel-enforced cap** (`--storage-opt size=`). Deferred until
  auth + untrusted-caller scenario materializes. Cite this handoff's
  threat-model section in any future ADR.
- **No tmpfs mount.** Workspace bind mount is load-bearing for the
  trusted-sample artifacts contract; tmpfs forces re-architecting
  where outputs persist. Out of scope.
- **No new `RunLimits` field for the poll interval.** Constant only.

## Independence

No upstream PR dependencies (P1.1 cidfile, P1.3 failure manifest are
all merged). Single PR.
