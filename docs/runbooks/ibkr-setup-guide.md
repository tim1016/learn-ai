# IBKR read-only evidence setup

**Scope:** Configure Interactive Brokers only as a read/evidence source for market
data, account data, completed orders, and capability diagnostics.

**Not an operator surface:** The IBKR bot launcher, evaluator, Account Clerk, and
order-actuation paths are retired. Do not use this guide to deploy, start, stop,
recover, cancel, or place an IBKR bot order. Current bot control is the Alpaca Broker
V2 panel and its [broker clerk fleet authority](../broker-clerk-fleet-authority.md).

## Safe configuration

1. Enable the IBKR API connection for the paper Gateway or TWS instance that supplies
   the evidence.
2. Keep the broker API and the data service in read-only mode. The repository default
   is `IBKR_READONLY=true`; do not change it for this integration.
3. Use a unique client ID for this read-only evidence connection. If IBKR reports a
   conflict, choose an unused ID or end the duplicate read-only session; do not use a
   client-ID collision as a reason to restart a legacy launcher.
4. Configure the host and paper port that match the Gateway/TWS process actually
   running. A live account or live port is a stop condition for this read-only
   integration unless the task is explicitly a live-evidence diagnostic.

Example `.env` values:

```dotenv
IBKR_MODE=paper
IBKR_HOST=host.containers.internal
IBKR_PORT=4002
IBKR_CLIENT_ID=7
IBKR_READONLY=true
```

`4002` is the usual Gateway paper port and `7497` is the usual TWS paper port. The
actual Gateway/TWS configuration is authoritative.

## Nightly relogin

IB Gateway logs itself out on its own nightly schedule. In IB Gateway, open
Configure -> Settings -> Lock and Exit, and enable **Auto restart**. This is
IBKR-side configuration, not a repository setting — there is no `.env` flag
for it.

With Auto restart off, Gateway does not reconnect after its nightly logout: the
data-plane clerk logs a connect failure roughly once a minute, and any bot
still running (including Alpaca bots — the IBKR feed supplies their live bars)
dies at its next decision trigger because the feed it streams from is gone.
Do not respond to this by disabling `IBKR_BROKER_ENABLED`; that flag controls
whether the shared market-data feed exists at all and is load-bearing for
Alpaca execution (see
[`docs/ibkr-integration-authority.md`](../ibkr-integration-authority.md)). Fix
the Gateway-side Auto restart setting instead.

## Verify the evidence path

1. Open the read-only broker health/capability view and confirm it reports the intended
   paper account and connection mode.
2. Confirm account, positions, open orders, completed orders, or bar snapshots can be
   read as required by the task.
3. When evidence is stale or unavailable, keep the affected downstream action blocked
   and correct the Gateway/TWS connectivity or API settings. Do not manufacture a
   broker write to test the connection.

For the boundary, retained routes, and current implementation evidence, use
[`docs/ibkr-integration-authority.md`](../ibkr-integration-authority.md). This guide
intentionally does not describe host-daemon process control: the surviving host daemon
is a constrained capability-observation bridge, not an IBKR bot supervisor.
