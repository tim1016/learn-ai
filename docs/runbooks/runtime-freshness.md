# Runtime Freshness Notices

Operator-facing playbook for the `runtime.*` notice family. The cockpit
banner renders these verbatim; this document is what links from
`runbook_slug`.

## Notices

### `runtime.market_closed`

The bot is idle until the regular trading session opens. No trading
decision is being made.

**Trader action:** none.

### `runtime.market_session_halted`

Exchange has halted the session for this symbol. The bot will resume
when the halt clears.

**Trader action:** monitor exchange notices.

### `runtime.market_data_stale`

The market-data feed is producing data but the most recent bar (or
heartbeat) is older than the freshness window. The bot holds new trading
decisions until fresh data arrives.

**Trader action:** if this persists past one minute, check the IBKR
client and the polygon data feed.

### `runtime.market_data_feed_stalled`

Both the heartbeat and the latest bar are stale. The data feed is fully
stalled. The bot holds new trading decisions until fresh data arrives.

**Trader action:** check the IBKR connection and the broker daemon
status.

### `runtime.broker_probe_stale`

The broker probe has not returned a fresh status within the freshness
window. The bot is protecting itself.

**Trader action:** monitor; if persistent, check the broker daemon.

### `runtime.broker_probe_missing`

The broker probe has not run since the bot started. Cockpit sees no
broker telemetry.

**Trader action:** check that the broker daemon is connected.

### `runtime.command_loop_unresponsive`

The control plane is not acknowledging commands. Pause / Resume / Stop /
Flatten may not take effect.

**Trader action:** stop the bot from the host runner and **manually
verify positions at IBKR**.

### `runtime.engine_runtime_incompatible`

The engine runtime version is incompatible with the cockpit. The bot
will not start trading.

**Trader action:** redeploy with a matching runtime.

### `runtime.control_plane_lease_stale`

Another control-plane lease holder hasn't checked in. The bot is in a
guarded state.

**Trader action:** verify only one cockpit/runner is attached to this
run.

### `runtime.control_plane_boot_id_mismatch`

The engine reports a different boot ID than the cockpit. A restart
happened that the cockpit did not initiate.

**Trader action:** **STOP** trusting cockpit state; reconcile positions
at IBKR; redeploy.
