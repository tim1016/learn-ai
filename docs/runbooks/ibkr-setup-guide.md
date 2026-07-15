# IBKR Setup Guide

Last verified: 2026-07-14

This guide covers the paper-trading setup this repo expects for IB Gateway or Trader Workstation. It is operational documentation, not financial advice. The application remains paper-first; live trading requires separately validated infrastructure.

## Official references

- IBKR Campus TWS API documentation: https://ibkrcampus.com/campus/ibkr-api-page/twsapi-doc/
- IBKR Campus installing and configuring TWS for the API: https://www.interactivebrokers.com/campus/trading-lessons/installing-configuring-tws-for-the-api/
- IBKR Campus essential components of TWS API programs: https://www.interactivebrokers.com/campus/trading-lessons/essential-components-of-tws-api-programs/

## Required IBKR API settings

In TWS, open Global Configuration -> API -> Settings. In IB Gateway, use the equivalent API settings panel.

Set these before connecting the app:

| Setting                           | Paper expectation                        | Why it matters                                                                                                                                             |
| --------------------------------- | ---------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Enable ActiveX and Socket Clients | Enabled                                  | The TWS API socket must be enabled before any client can connect.                                                                                          |
| Read-Only API                     | Disabled for order-capable paper testing | IBKR enables read-only mode by default in some flows. Leave it enabled only for read-only diagnostics; disable it before order-capable paper tests.        |
| Socket port                       | TWS paper: `7497`; Gateway paper: `4002` | IBKR's standard paper ports differ between TWS and Gateway.                                                                                                |
| Socket port, live reference only  | TWS live: `7496`; Gateway live: `4001`   | The repo's paper guardrails should prevent live-port order paths. Treat a live port as a safety incident unless explicitly testing a read-only diagnostic. |
| Client ID                         | Unique per simultaneous API client       | IBKR rejects or drops sessions when another API client is already using the same client ID.                                                                |

## Repo configuration

Use `.env` values that match the IBKR process you actually launched:

```dotenv
IBKR_MODE=paper
IBKR_HOST=host.containers.internal
IBKR_PORT=4002
IBKR_CLIENT_ID=7
IBKR_READONLY=false
```

Notes:

- `host.containers.internal` is preferred when PythonDataService runs in a container and IB Gateway/TWS runs on the host Mac.
- `IBKR_PORT=4002` is the normal paper Gateway port. Use `7497` if connecting to paper TWS instead.
- Give the data-plane connection and any live-runner/host-runner connection different client IDs. Do not reuse the same client ID for two active sessions.
- Keep the paper account sentinel visible: paper accounts should present the `DU` prefix in app health.

## Client-ID separation

IBKR's API connection signature includes host, port, and client ID. The client ID is not a secret; it is a connection slot. Use stable, non-overlapping ranges:

| Consumer                        | Suggested client ID range |
| ------------------------------- | ------------------------- |
| Frontend/data-plane diagnostics | `7-19`                    |
| Host runner / bot-owned session | `20-49`                   |
| Manual scripts and notebooks    | `100+`                    |

If Broker Status reports client-ID overlap, do not retry blindly. Stop the duplicate client, choose another client ID, or restart Gateway/TWS to clear stale sessions.

## Diagnostic flow

1. Open Broker Status.
2. Confirm Mode is `PAPER`, the account is a `DU...` paper account, and the port matches paper Gateway/TWS.
3. Read the Effective IBKR configuration card. It is display-only.
4. Click Diagnose if the socket does not connect or reconnects unexpectedly.
5. If diagnostics mention client ID, socket, Trusted IPs, API settings, or read-only mode, fix the Gateway/TWS setting first, then reconnect.
6. After reconnect, open Account Monitor and run account reconciliation before starting bots.

## Common failures

| Symptom                               | Likely cause                                                         | Operator move                                                                                          |
| ------------------------------------- | -------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| Connection refused                    | Gateway/TWS is not running, wrong host, or wrong port                | Start the IBKR app and confirm the configured paper port.                                              |
| Socket connects then drops            | API setting rejection, Trusted IP issue, or duplicate client ID      | Recheck API settings and choose a unique client ID.                                                    |
| Orders are rejected as read-only      | Read-Only API is still enabled or `IBKR_READONLY=true`               | Keep diagnostics read-only, but switch both IBKR and `.env` to order-capable before paper order tests. |
| App shows live mode or non-DU account | Wrong IBKR session or live port/account                              | Stop. Disconnect and reconnect to the intended paper account before using broker pages.                |
| Account Monitor remains frozen        | Open or unattributed exposure still exists, or account proof expired | Flatten/audit exposure, then run account reconciliation again.                                         |

## Cutover checklist for market-hours evidence

Before enabling any real code path that depends on live broker evidence:

- Broker Status shows paper mode, paper port, and `DU` account sentinel.
- Effective IBKR configuration shows the expected host, port, and data-plane client ID.
- No client-ID overlap warning is active.
- Account Monitor has a fresh account reconciliation receipt.
- Unattributed exposure is flat or has an audited accepted override.
- Any retired-bot recovery row is resolved or deliberately left frozen.

Do not replace these checks with dummy production code. Pre-market work can add read-only display and disabled affordances; order-changing paths should wait for market-hours evidence.

## Account Clerk restart smoke for observation-lease cutover

This human-in-the-loop smoke is one of the promotion requirements for the
Account Observation Lease. Run it only against a `DU...` paper account during a
supported NYSE session. It validates Clerk recovery; it does **not** authorize
changing `IBKR_ACCOUNT_GATE_AUTHORITY` from its default `account_truth`.

### Preconditions

- Complete the market-hours cutover checklist above and keep the account free
  of unattributed exposure, unresolved recovery incidents, and unknown working
  orders.
- Keep exactly one approved paper-validation bot active for the target account.
  Do not use a live bot, emergency flatten, direct IBKR API calls, or a second
  writer to manufacture the test.
- From the host-daemon health view, record the target row in `clerks[]`:
  `account_id`, `generation`, `pid`, `status`, `renewed_at_ms`,
  `valid_until_ms`, and `lease_valid`. Before the restart it must be one
  `RUNNING` Clerk with `lease_valid=true`.
- Record the current account-reconciliation receipt and the current
  `account_clerk` evidence from the bot status view. If a normal submit was
  already journaled, note its intent/order reference; do not create a new order
  solely to test recovery.

### Procedure

1. Use the approved host deployment/process-supervisor procedure to restart
   the target **Account Clerk**. Do not kill a PID by hand or invoke a direct
   broker-write path. The host supervisor owns Clerk lifecycle, lock handling,
   client-ID quarantine, and generation advancement.
2. During the transition, treat absent, expired, draining, or
   generation-mismatched Clerk/lease evidence as a hard stop. Do not start or
   resume another bot and do not retry a submit merely because a socket exists.
3. Wait for the host-daemon health view to show exactly one replacement row for
   the account. It must have a strictly higher `generation`, `status=RUNNING`,
   and `lease_valid=true`. The replacement must have completed its generation
   RPC handshake; a generation file or socket alone is not proof of readiness.
4. Refresh Account Monitor and run account reconciliation. Confirm a fresh
   post-restart Account Truth receipt, no client-ID overlap/orphan-socket
   warning, and `account_clerk` evidence whose accepting generation matches the
   active Clerk lease.
5. Resume only the approved paper-validation bot. Confirm the next normal
   submit boundary records a schema-v2, Clerk-keyed shadow comparison. If an
   intent existed before the restart, verify its durable journal/receipt is
   represented once rather than replayed as a second broker action.

### Pass and stop conditions

Pass only when the replacement Clerk is healthy, the reconciliation and
post-restart lease are fresh, there is no second broker writer or orphaned
socket, and the observed shadow comparison is not lease-weaker. Preserve the
daemon health snapshot, reconciliation receipt, and account-events journal for
the three-session parity archive.

Stop and leave the account blocked if the old generation remains usable, the
replacement generation cannot complete its RPC handshake, the lease is not
valid, broker/session evidence is ambiguous, or the lease allows a submit that
Account Truth blocks. Resolve and document the incident before another paper
session; never bypass the failure with a per-bot authority override.
