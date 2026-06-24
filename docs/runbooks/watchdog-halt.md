# Watchdog Halt Runbook

**Runbook slug:** `watchdog-halt`  
**Applies to:** All live-trading engine runs under daemon supervision.

---

## What is a watchdog halt?

The engine's `ChildWatchdog` polls the daemon lease file every second.
If the lease is not refreshed within `5 000 ms` (or the daemon's `boot_id`
changes), the watchdog triggers a controlled halt:

1. **Block submissions** — no new orders leave the engine.
2. **Persist durable PAUSED** — the run's desired state is durably set to `PAUSED`.
3. **Flatten positions** — the engine attempts to close all open positions (20 s timeout).
4. **Disconnect broker** — the IBKR session is cleanly disconnected (3 s timeout).
5. **Request engine exit** — the bar loop is stopped.

A 5-second "suspected loss" grace window absorbs transient flaps: if the lease
recovers within those 5 seconds, the watchdog returns to `HEALTHY` with no
incident written and no halt triggered.

Every halt writes a typed `OperatorIncident` to
`<run_dir>/operator_incidents/<incident_id>.json`.

---

## Notice codes and what they mean

### `watchdog.flatten_completed` (info)

The halt ran cleanly. All positions were flattened and the broker was disconnected
in an orderly fashion. **No manual action is required.** The account is in a
clean state.

**Recovery:** Start a new run from the cockpit. The post-halt gate will not block.

---

### `watchdog.flatten_not_needed` (info)

The halt ran cleanly. The account held no open positions, so no flatten was
needed. **No manual action is required.**

**Recovery:** Start a new run from the cockpit. The post-halt gate will not block.

---

### `watchdog.flatten_timed_out` (critical)

The flatten operation did not complete within the 20-second deadline. The broker
session was then forcefully disconnected. **Open positions may still exist in your
IBKR account.**

**Recovery procedure:**
1. Log into Trader Workstation (TWS) or IBKR web portal.
2. Check the account's open positions for the strategy's symbol.
3. If residual positions exist, close them manually.
4. Return to the cockpit and click **Reconcile now**.
5. Once reconciliation passes, start a new run.

The post-halt gate in `cmd_start` will refuse to start trading until the
reconciliation gate is cleared.

---

### `watchdog.flatten_failed` (critical)

The flatten operation raised an exception and did not complete. The broker session
was then forcefully disconnected. **Open positions may still exist in your IBKR
account.**

**Recovery procedure:** Same as `watchdog.flatten_timed_out` above.

---

### `watchdog.broker_disconnected_before_flatten` (critical)

The broker connection was lost before the flatten operation could run. The engine
did not have a chance to close open positions. **Open positions may still exist
in your IBKR account.**

**Recovery procedure:** Same as `watchdog.flatten_timed_out` above.

---

## Post-halt gate

When a halt leaves one of the three critical notices above unresolved, the engine's
startup path (`cmd_start`) checks for it via the **post-halt reconciliation gate**
(`app.engine.live.post_halt_gate.check_post_halt_gate`). If the gate detects a
blocking incident:

- `cmd_start` exits with code `1` (fatal halt) and prints:

  ```
  [START] HALT post_halt_gate: reconciliation.required_after_uncertain_flatten
  (prior watchdog halt left broker state uncertain). Use Reconcile-now before restarting.
  ```

- The cockpit surfaces a `reconciliation.required_after_uncertain_flatten` notice.

**Clearing the gate:**

1. Verify and close any residual IBKR positions manually (see critical-tier
   recovery above).
2. Click **Reconcile now** in the cockpit.  The reconciliation orchestrator
   (`reconciliation_orchestrator.reconcile`) runs and writes a receipt.
3. Once the receipt shows `passed`, the `OperatorIncident` is marked resolved
   by the reconciliation service and the gate clears.
4. Start a new run from the cockpit.

---

## Where incidents are stored

```
<run_dir>/operator_incidents/<incident_id>.json
```

- `incident_id` format: `watchdog-<started_at_ms>-<8 hex chars>`
- Each file is a JSON-serialized `OperatorIncident` (Pydantic schema v1).
- Written atomically (tmp + fsync + os.replace + parent fsync).

---

## Related files

| File | Role |
|---|---|
| `app/engine/live/child_watchdog.py` | Watchdog state machine + halt handler |
| `app/engine/live/watchdog_controller.py` | `WatchdogHaltExecutor` — 5-step sequencer |
| `app/operator/incidents/store.py` | Atomic incident writer + reader |
| `app/operator/incidents/watchdog_notices.py` | Notice builders per terminal outcome |
| `app/engine/live/post_halt_gate.py` | Startup gate that checks for blocking incidents |
| `app/operator/notices/schema.py` | `OperatorIncident` / `OperatorNotice` schemas |
