# Cross-Client Execution Runbook

**Runbook slug:** `cross-client-execution`  
**Applies to:** All live-trading engine runs with IBKR connectivity.

---

## What does this alert mean?

The broker-activity publisher performs a periodic `reqExecutions` sweep
every 60 seconds (lookback: 900 seconds).  When the sweep finds a fill
on this IBKR account that was **not initiated by this bot** — no matching
engine intent by `order_ref` or `perm_id` — it emits a
`reconciliation.discovered_execution_not_in_engine_state` critical incident.

Possible causes:

1. **Manual TWS click** — you (or another operator) placed an order
   directly in Trader Workstation while the bot was running.
2. **Shared paper account** — another strategy instance (different
   `strategy_instance_id`) placed an order on the same IBKR DU account.
   This is expected; each instance's sweep only watches its own namespace,
   so a fill from a different bot will appear foreign to this one.
3. **Stale order from a previous session** — an order placed before the
   current bot run completed after the run started.  The `exec_time_ms`
   field in the forensic-facts panel shows when the fill executed; compare
   against the run's start time.
4. **Account compromise or IBKR back-office action** — rare, but possible.

---

## Immediate actions

1. **Do not restart the bot** until you understand the source of the fill.
2. **Check Trader Workstation (TWS) or the IBKR web portal:**
   - Open the Account → Portfolio view.
   - Confirm the positions match what the bot's bot control shows.
   - Review the Trades log for the fill's `exec_id` (shown in the
     bot control's forensic-facts panel under "Broker activity").
3. **Identify the source:**
   - If it was your own manual trade: close or reconcile the position
     manually, then use the bot control page **Reconcile now** button.
   - If it was another bot instance on the same account: verify the other
     instance's activity log and confirm the fill belongs to it.
   - If the source is unknown: **halt the bot immediately** (bot control →
     Stop) and escalate.

---

## Recovery procedure

After you have identified and resolved the fill's source:

1. Ensure the bot's IBKR positions match what you expect.
2. In the bot control page, click **Reconcile now**.  The reconciliation
   orchestrator runs and writes a receipt.
3. Once the receipt shows `passed`, the incident is marked resolved and
   the incident headline clears.
4. Resume or restart the run from the bot control page.

---

## What the bot does (and does not do)

- **Does:** Author an `unmatched_execution` row in the broker-activity log
  so you can see the fill's forensic detail (exec_id, symbol, side,
  quantity, price, discovery timestamp).
- **Does:** Persist a critical `OperatorIncident` that surfaces in the
  bot control's operator-surface headline.
- **Does NOT:** Silently correct the bot control page's position view.
- **Does NOT:** Cancel or reverse the fill automatically.
- **Does NOT:** Block new orders — the periodic sweep is background
  bookkeeping, not a submission halt.  If you want to halt submissions
  while investigating, use the bot control page's Stop action.

---

## Forensic facts in the incident

| Field | Meaning |
|---|---|
| `exec_id` | IBKR's globally unique execution identifier |
| `perm_id` | IBKR permanent order ID (may be `None` for some order types) |
| `order_ref` | `orderRef` string echoed by IBKR (empty for manual TWS orders) |
| `symbol` | Instrument symbol |
| `side` | `BUY` or `SELL` |
| `quantity` | Fill quantity |
| `price` | Fill price |
| `discovered_at_ms` | Wall-clock milliseconds when the sweep discovered the fill |

---

## Related files

| File | Role |
|---|---|
| `app/services/broker_activity_publisher.py` | Periodic sweep + incident emitter |
| `app/operator/incidents/store.py` | Atomic incident writer |
| `app/operator/notices/schema.py` | `OperatorIncident` / `OperatorNotice` schemas |
| `app/services/broker_activity_reconciler.py` | `match_identity` — engine-intent resolution |
| `app/broker/ibkr/orders.py` | `executions_for_reconnect_recovery` — fill fetcher |
