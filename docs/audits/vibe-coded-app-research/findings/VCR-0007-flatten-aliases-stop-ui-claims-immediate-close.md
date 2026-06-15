---
id: VCR-0007
severity: P1
status: remediated
area: halt-state-machine
canonical_file: PythonDataService/app/engine/live/live_engine.py
reference: docs/architecture/adrs/0004-instance-addressed-operator-control-plane.md
first_seen: 2026-06-14
last_seen: 2026-06-14
remediated_in: "#498 — Phase 6A — Flatten-and-pause composition (ADR 0010 v1 contract)"
regrounded_to: high
lens: halt-pause-stop-flatten-poison
dedupe_with_F: none
confidence: high
---

## What

The `FLATTEN` command verb is documented as "currently aliases to STOP" inside `live_engine.py` and persists `STOPPED` to durable desired-state. The actual flatten only fires through `_shutdown_flatten`, which depends on the bar-loop honoring `shutdown_event`. The cockpit UI advertises the verb as "Close all open positions immediately" with no signal that:

1. The bot will also be **stopped** (durable intent transitions to `STOPPED`).
2. The bot will refuse to restart until the operator explicitly redeploys or rewrites durable desired-state.

The label promises an action ("close positions"), but the runtime delivers a different effect ("close positions and stop").

## Where

- `PythonDataService/app/engine/live/live_engine.py` — `FLATTEN` dispatcher branch (described in lens summary as "aliases to STOP"; specific line range to verify on next sweep).
- `PythonDataService/app/engine/live/command_channel.py` — verbs `PAUSE`/`RESUME`/`STOP`/`FLATTEN`/`RECONCILE`/`MARK_POISONED`.
- `PythonDataService/app/engine/live/desired_state.py` — durable `RUNNING/PAUSED/STOPPED` persistence.
- `PythonDataService/app/engine/live/live_engine.py::_shutdown_flatten` — the actual flatten executor.
- `Frontend/src/app/components/broker/broker-instances/**` — the cockpit's FLATTEN button labels.

**Note**: this finding was surfaced in lens prose; the specific FLATTEN dispatcher line range was not independently re-verified by the main loop. Marked `confidence: medium` for that reason. The runtime semantics (FLATTEN = STOP + shutdown_flatten) and the UI label are the load-bearing claims; both should be re-grounded before remediation.

## Why this severity

PRD §7 P1: "UI implies guarantees the backend/runtime does not enforce." The operator-facing affordance "Close all open positions immediately" is one of the highest-stakes labels in the cockpit — it's the panic button. If the operator does not realize that pressing FLATTEN also stops the bot, they may expect it to resume trading after the flatten completes (it will not), or they may use FLATTEN in scenarios (intra-session de-risk, EOD square-up) where they actually want flatten-and-keep-running.

Not P0 because FLATTEN still does close positions (the bar-loop sees the shutdown signal and `_shutdown_flatten` runs). The gap is between the labeled affordance and the actual effect, not silent corruption.

## Trading impact

- **Operator surprise** in the most stress-prone moment of operation (panic flatten). The operator expects "close positions" and gets "close positions and bot is permanently stopped until manual restart".
- **Recovery friction**: re-enabling the bot requires either redeploy (new `run_id`) or hand-editing durable desired-state.
- **No FLATTEN-without-STOP primitive** exists. A scheduled de-risk operation (e.g., square up before earnings) has no corresponding command.

## Reproduction

```bash
# Confirm FLATTEN exists in the command verbs:
grep -nE 'FLATTEN|flatten' PythonDataService/app/engine/live/command_channel.py | head

# Confirm the dispatcher path in live_engine:
grep -nE 'FLATTEN|_shutdown_flatten|shutdown_signalled' PythonDataService/app/engine/live/live_engine.py | head

# Confirm UI labels:
grep -rn 'Close all open positions\|FLATTEN\|Flatten' Frontend/src/app/components/broker/
```

## Suggested resolution (NOT auto-applied)

Two reasonable paths:

1. **Add a true FLATTEN-without-STOP primitive.** Separate `_shutdown_flatten` from durable-state mutation. New `FLATTEN` command leaves the bot in `RUNNING` after the flatten completes; the operator can choose to also issue `STOP` separately. Update UI label to "Close all open positions (continues running)".
2. **Keep the current aliasing but rename the UI.** If the team prefers FLATTEN-implies-STOP semantics, change the label to "Stop and close all open positions" so the operator sees both effects before clicking.

In either case:
- Add a confirmation modal that distinguishes the two outcomes.
- Add a runtime test asserting the dispatched command's downstream durable-state mutation matches the documented contract.

## Provenance of the finding

Lens: `halt-pause-stop-flatten-poison` (workflow `wf_def78013-ce4`). Surfaced in lens prose; live_engine FLATTEN dispatcher line range not independently re-verified by the main loop. Re-ground before remediation.
