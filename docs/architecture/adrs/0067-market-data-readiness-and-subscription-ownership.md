# ADR 0067 — Server-owned market-data readiness and recovery

**Status:** Accepted 2026-09-22
**Provenance:** Implements the owner's request for robust market-liveness behaviour, delivered as a reviewable pull request.
**Vocabulary:** `CONTEXT.md` § "Market-data readiness (resolved 2026-09-22)".

The clerk owns market-data subscriptions for deployed bots, running feeds, and
custody positions and working orders. Browser observation neither creates nor
renews trading evidence. Connection health, subscription readiness, reported
halt state, and decision-data freshness are separate facts. This supersedes
ADR 0062's **not-halted tick OR recent trade** readiness rule; its IBKR data /
Alpaca execution provider decision is unchanged.

## Evidence and admission

Each subscription generation begins without usable price evidence. Actual
IBKR callbacks record receipt times; reading a cached ticker or publishing a
snapshot never advances them. A generation is READY only with a valid live
two-sided quote or a vendor-timestamped RTVolume trade within the existing
5,000 ms decision-data budget. Both quote sides must have current receipts;
trade vendor time and local receipt must both be current. Delayed/frozen data,
request rejection, future-dated data, expired evidence, and a disconnected
source refuse new exposure. A reconnect or subscription replacement requires
new callbacks; old-generation callbacks cannot restore readiness.

A missing initial halt tick remains **not reported**, not an invented clear
status. Positive data readiness can permit this case. An explicit unavailable
status blocks. A reported halt is latched, persisted atomically in the clerk's
artifacts, and cleared only by an explicit live not-halted tick. Fresh prices,
reconnection, and process restart cannot clear it. Invalid retained evidence
refuses startup; persistence failure blocks admission. The IBKR adapter
preserves tick 49's raw -1/0 distinction because the installed ib_async generic
size decoder otherwise collapses unavailable into clear. Data-type callbacks
also invalidate readiness even without a price tick.

Start, Resume, strategy entries, and the clerk use the same composed fact. The
clerk rechecks it immediately before entering the broker's submission method,
and refuses an intervening generation change even if the new generation is
already healthy. A refusal at this boundary records a known failed submission;
it does not claim an ambiguous broker outcome. Exit/reduction policy is
unchanged. A fact's assessment timestamp is current; constituent timestamps
and the evidence deadline remain intact, so a specific refusal is not hidden
behind a generic stale-authority message.

## Ownership and recovery

Durable roster deployments keep subscriptions through Stop and process restart;
retiring or archiving a flat deployment removes that demand. Active feeds and
custody positions/orders have priority, followed by deployments, then explicit
Start/Resume/limit-price preparation requests (60-second leases). Capacity is
bounded to 64 symbol requests per clerk and four simultaneous qualifications;
excess demand is explicitly unavailable. A slow qualification cannot hold up
healthy symbols or clock publication. These bounds fit inside the usual IBKR
market-data line budget but do not reserve capacity used by other connections;
a vendor capacity refusal stays visible.

The independent supervisor reconciles every second. Qualification has a
three-second deadline. Thirty seconds without any subscription callback triggers
repair, with retries delayed by 1, 2, 4, 8, 16, then at most 30 seconds. Readiness
already expires at its original decision-data deadline; the repair budget grants
no extra trading time. Explicit subscription/permission refusals remain
unavailable until demand is released and reacquired or the connection generation
changes. They are not hammered with blind retries.

IBKR 1100 immediately invalidates evidence. Recovery with 1101 recreates requests;
1102 reuses maintained requests but fences prior receipts and requires new
callbacks. Socket and relevant farm transitions also invalidate the generation.
Halt memory survives every path. Authenticated shared snapshots carry generation,
receipt times and deadlines unchanged. Snapshot publication and broker-clock
freshness each retain a separate 5,000 ms bound; one constant is not treated as a
vendor heartbeat.

## Trade-offs and validation

Increasing the last-trade timeout or counting failed UI polls would preserve the
wrong ownership and make safety depend on evaluation frequency. Watchlist
RTVolume is event-driven; a five-second real-time-bar delivery interval is not
its heartbeat. A quiet trade tape with a current book can be ready, while a
clear halt flag with dead prices cannot. A quiet instrument with neither recent
quote nor trade remains blocked under the existing decision-data policy. This
change does not assert a universal safe price-age budget for every strategy.

Regression coverage includes quiet trades with live quotes, pure panel reads,
unchanged receipt deadlines, no-UI deployment/working-order ownership, stalled
and slow subscriptions, reconnect fencing, retained halts, raw unavailable halt
ticks, delayed/frozen transitions, request refusal and last-moment order refusal.
No runtime dependencies or paid data sources are added. The PR does not deploy
or place orders. Paper validation must still exercise navigation and Gateway
loss/recovery during RTH, recording state, generation, receipt ages, readiness
deadlines, cancellations and subscription counts before Live rollout.

## Vendor references

- [IBKR market-data update frequency](https://www.interactivebrokers.com/docs/tws-api/doc/market-data-live/top-of-book-l-1/market-data-update-frequency): stock watchlist updates and their cadence are distinct from real-time bars.
- [IBKR halted ticks](https://www.interactivebrokers.com/docs/tws-api/doc/market-data-live/available-tick-types/halted): -1 unavailable, 0 not halted, 1/2 halted; initial clear reporting is watchlist-dependent.
- [IBKR RTVolume](https://www.interactivebrokers.com/docs/tws-api/doc/market-data-live/available-tick-types/rt-volume): generic subscription 233 and trade timestamps.
- [IBKR connectivity codes](https://www.interactivebrokers.com/docs/tws-api/doc/error-handling/system-message-codes): 1100, 1101 and 1102 recovery semantics.
