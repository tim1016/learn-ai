---
id: VCR-0018
severity: P2
status: partially_remediated
area: ui-runtime-claims
canonical_file: multiple Frontend broker components
reference: PRD §7
first_seen: 2026-06-14
last_seen: 2026-06-14
remediation_progress:
  - "#499 — Phase 6B — VCR-0018-B Stop ack semantics (signal-accepted vs process-exited)"
  - "#500 — Phase 6C — VCR-0018-F Engine-level force-flat enforcement"
  - "#501 — Phase 6D — VCR-0018-G Per-instance start lock + halt.flag pre-flight rerun"
follow_up_required:
  - "VCR-0018-A — Sentinel pill label bound to structured 4-layer verdict (Phase 7B)"
  - "VCR-0018-C — ReadinessGate label coverage gap (Phase 7 mechanical)"
  - "VCR-0018-D — Deploy form 'Live mode' dialog wording (Phase 7B)"
  - "VCR-0018-E — sub-item to verify against current code"
  - "VCR-0018-J/K/L/N — timestamp rigor + QC card tail (Phase 7 mechanical)"
lens: ui-vs-runtime-claims
dedupe_with_F: none
confidence: medium
---

## What

Rollup of smaller UI-vs-runtime mismatches that don't warrant individual finding files but degrade operator decision quality.

### A — Sentinel pill label mode-blind (P1 candidate, rolled up)

The "DU prefix matches paper" sentinel pill label is rendered unconditionally OK as long as the account string starts with `DU`. It does not consult the broker's actual `mode` or port. In a hypothetical `mode=live` deploy that still binds to a paper account (a misconfig case), the pill would show OK while the rest of the system is in an inconsistent state.

**Path**: bind the sentinel pill to the structured 4-layer verdict (mode + port + readonly + DU prefix) rather than just the prefix.

### B — Stop/daemon contract returns `accepted=true` after 2s timeout

In non-force mode, the stop command returns `accepted=true` after a 2-second window even if the process has not actually exited. The cockpit treats `accepted=true` as success and updates the UI optimistically. A child process that ignores SIGTERM (e.g., wedged in a synchronous `await` on the broker) would leave the operator believing it stopped.

**Path**: poll `process_registry` for actual exit before returning `accepted=true`; on timeout, return `accepted=false, reason="still_running_after_2s"` and surface that distinction in the UI.

### C — `ReadinessGate` label coverage gap (5 of 8 server-emitted gates)

The Frontend's `READINESS_GATE_LABELS` constant covers only 3 of the 8 readiness gates the server emits. The other 5 render with their raw enum names (e.g., `data_quality_freshness`, `intent_wal_intact`) — operator-facing strings rendered as developer-facing enum tokens.

**Path**: expand `READINESS_GATE_LABELS` to cover all 8. Add a TS lint that fails if a new enum value lands without a label.

### D — Deploy form "Live mode can place real orders" dialog ambiguous

The deploy form's mode dialog warns about real orders, but the IBKR_MODE=paper enforcement at the runtime means the warning is unconditional regardless of whether the operator actually selected a live-capable path. The wording implies the dialog is conditional on live mode being selected; it is not.

**Path**: render the dialog only when `mode === "live"` selected (or a live-port is in the API response), and update the wording to be specific about the trigger.

### E — Start card "Historical Data Loading" label conflates indicator-state hydration with bar history

The Start card's "Historical Data Loading" label fires during indicator-state hydration, which is a different concept from loading historical bars for backfill. An operator reading this expects "fetching the last N days of bars"; the runtime is actually replaying indicator state from a prior session.

**Path**: rename the label to "Indicator state hydration (replaying prior session)" with a tooltip explaining the difference.

### F — Force-flat readiness gate session_window FAIL=hard but bar-loop keeps running

`ReadinessGate.session_window` returns FAIL=hard when force-flat is active. The UI shows the bot as BLOCKED. But the bar loop does **not** install a "flat-only" guard for the rest of the session: `strategy.on_minute_bar` keeps running, and any orders it queues are still submitted (subject to the policy adapter). Enforcement currently relies on each strategy's `on_force_flat()` resetting its own internal state to a no-emit mode — the base `Strategy` class is a docstring-only no-op, so individual algorithms must remember to override it. `SpyEmaCrossoverAlgorithm` does not override it.

**Path**: install an engine-level flat-only guard in `LiveEngine.run` that, when `desired_state == FORCE_FLAT` (or whatever the readiness gate consults), refuses to call `strategy.on_minute_bar` until the next session window opens. The strategy's own opt-in `on_force_flat` is fragile.

### G — `_fatal_halt` and `_persist_desired_state` swallow write failures

Both functions catch write exceptions with `logger.exception(...)` and proceed as if persistence succeeded. The `FatalHaltError` is raised but the next start of the same `run_id` reads no `poisoned.flag`, and the command channel acks "success: paused" even when the durable PAUSED intent never reached disk.

**Path**: re-raise on persistence failure (or transition to a tighter degraded mode), so the operator sees a failed halt rather than a silent one.

## Where

- `Frontend/src/app/components/broker/broker-status/` — sentinel pill template.
- `PythonDataService/app/engine/live/run.py::cmd_stop` (or equivalent) — 2s timeout pattern.
- `Frontend/src/app/components/broker/**` — `READINESS_GATE_LABELS` constant.
- `Frontend/src/app/components/broker/broker-deploy-form/` — mode dialog.
- `Frontend/src/app/components/broker/broker-start-stop-card/` — historical data loading label.
- `PythonDataService/app/engine/live/live_engine.py` — force-flat bar-loop semantics.
- `PythonDataService/app/engine/live/live_engine.py::_fatal_halt`, `_persist_desired_state` — swallowed write failures.

## Why this severity

PRD §7 P2: each item individually is moderate auditability / UI-claim drift. Item A and F have P1-leaning trading impact (sentinel pill blind to mode, force-flat not actually flat-only) but are rolled up here because the cause is the same class (UI claims a guarantee the runtime delivers conditionally) and they share the same fix pattern (bind UI labels to structured server-side verdicts and enforce flat-only at the engine level).

## Suggested resolution (NOT auto-applied)

Each item has its own resolution above. Two cross-cutting fixes:

1. **Server-side structured verdicts** for paper-mode, readiness gates, stop/exit status. The Frontend binds to those, not to hardcoded labels or short-window heuristics.
2. **Engine-level enforcement** of state-machine invariants (force-flat = no bar-loop calls, halt = no order submit, etc.) — push the gate from "every strategy remembers to override" to "the engine refuses".

## Provenance of the finding

Lens: `ui-vs-runtime-claims` + `halt-pause-stop-flatten-poison` (workflow `wf_def78013-ce4`). Surfaced in lens summaries; specific component/line ranges not re-verified by the main loop for each item. Confidence `medium` per-item pending re-grounding before remediation.
