# Broker Session Orphaned Socket

An orphaned broker socket means the broker-session mirror can attribute an
IBKR socket to a bot run, but the host process that should own that socket is
not live.

## Operator Guidance

1. Open the owning Bot Cockpit from the notice.
2. Verify the corresponding client session in IB Gateway.
3. Reconcile open orders, executions, and positions before restarting the bot.
4. If the session still blocks reuse of the client id, use host/Gateway
   remediation for the whole Gateway session or wait for IBKR timeout.

There is no safe surgical "close this one client socket" action in the IBKR
API. Do not treat log purge or mirror refresh as remediation; they only affect
diagnostic visibility.
