---
id: VCR-0008
severity: P1
status: remediated
area: ui-runtime-claims
canonical_file: PythonDataService/app/engine/live/live_engine.py:1086
reference: docs/architecture/adrs/0005-engine-authored-readiness-two-altitude-broker-ownership.md
first_seen: 2026-06-14
last_seen: 2026-06-14
remediated_in: "#496 — Phase 4 — Remove runtime RECONCILE affordance + ADR 0008 banner"
lens: halt-pause-stop-flatten-poison
dedupe_with_F: none
confidence: high
---

## Remediation (#496 / Phase 4)

Closed by issue #496. The bot control page no longer renders the "Re-sync now"
button or routes the per-gate "Fix this" affordance through ``RECONCILE``;
the runtime verb on ``command_channel`` is kept as a backend-compat
surface but its dispatcher returns the structured no-op the PRD
specifies:

```json
{"result": "accepted_noop",
 "reason": "runtime_reconcile_not_wired",
 "manual_action": "restart_required_no_broker_refresh_occurred"}
```

The ``latest_reconcile`` gate's "Fix this" now reveals manual-restart
guidance instead of dispatching. Active runs render a banner above the
dashboard repeating the same wording so an operator who does not click
"Fix this" still sees the contract:

> Runtime reconcile is not wired yet. After a crash / restart or any
> suspected broker drift, stop the bot, verify the broker positions
> match the bot control page, and only then restart.

Phase 5B will promote this to a real durable "Schedule reconcile on
next restart" affordance once ``ColdStartReconciler.verify()`` is
wired into ``cmd_start``.

---

## What

The bot control page exposes a "Re-sync now" / "Re-sync account balance with broker" action that dispatches `RECONCILE` through the command channel. The bot control page's success notice reads *"the bot refreshes account balances on its next loop"*, and the per-gate "Fix this" wiring also routes through the same verb.

`live_engine.py:1086` reads, verbatim:

> *"RECONCILE is a runtime no-op — the ColdStartReconciler is the …"*

The runtime ACKs the command (`accepted=true`), persists nothing, and changes no broker- or engine-side state. The reconciliation envelope the UI promises is the cold-start path (ADR 0005), which only runs on engine boot — not in response to a runtime command. So the operator presses "Re-sync now", sees a green tick, and the system has done nothing.

This is paired with related VCR-0002 (the cold-start reconciler itself is not wired to actually run at boot either). But the UI claim here stands independently: even if the cold-start path were wired, the runtime verb the bot control page dispatches would still be a no-op.

## Where

- `PythonDataService/app/engine/live/live_engine.py:1086` — verbatim "RECONCILE is a runtime no-op — the ColdStartReconciler is the…" comment.
- `PythonDataService/app/engine/live/command_channel.py` — `RECONCILE` verb defined.
- `Frontend/src/app/components/broker/**` — "Re-sync now" / "Re-sync account balance with broker" labels and success notices.
- `PythonDataService/app/engine/live/cold_start_reconciler.py` — the actual reconciler (boot-time only, not currently invoked — see VCR-0002).

## Why this severity

PRD §7 P1: "UI implies guarantees the backend/runtime does not enforce." This is one of the cleanest examples of the pattern: the action button does nothing while the success notice asserts that it did something. Operator decisions downstream (e.g., "I just re-synced, so my view is fresh") are based on a lie.

Not P0 because no order or position math depends on the false success — but every operator-decision-quality criterion P1 covers is in scope.

## Trading impact

- **Stale view masquerading as fresh**: the operator presses "Re-sync now" before issuing a trade decision, sees the success notice, and trusts the displayed numbers — but they are exactly as stale as they were before the click.
- **"Fix this" links lie**: gates that surface "Fix this — re-sync" walk the operator to the no-op verb and report success.
- **Audit trail confused**: the command channel records ACKs that did not produce state mutations; a reviewer reading the operator-action log later cannot distinguish "operator did re-sync and it worked" from "operator did re-sync and nothing changed".

## Reproduction

```bash
# Confirm the runtime no-op comment exists verbatim:
sed -n '1083,1090p' PythonDataService/app/engine/live/live_engine.py

# Confirm RECONCILE verb in command_channel:
grep -n 'RECONCILE' PythonDataService/app/engine/live/command_channel.py

# Confirm UI labels:
grep -rn 'Re-sync\|reconcile\|noop_at_runtime' \
  Frontend/src/app/components/broker/
```

## Suggested resolution (NOT auto-applied)

Two paths:

1. **Make the verb do what the UI promises.** Wire a runtime reconciliation step that re-fetches broker positions + open orders, compares against engine state, and emits a divergence event. Reuse the `ColdStartReconciler` invariants but in the running-engine context. Surface the outcome (clean / divergence details / halt) instead of an unconditional success notice.
2. **Tell the truth in the UI.** Change the verb label to "Mark for re-sync at next restart" (with a tooltip describing the cold-start reconciler), and change the success notice to "Marked. The bot will reconcile on the next start." Remove the per-gate "Fix this" route to RECONCILE for any gate that should mutate runtime state.

Option (1) is the right end state but depends on VCR-0002 being closed; option (2) is the immediate honesty fix.

## Provenance of the finding

Lens: `halt-pause-stop-flatten-poison` (workflow `wf_def78013-ce4`). Verified by direct read of `live_engine.py:1086` (verbatim "runtime no-op" comment).
