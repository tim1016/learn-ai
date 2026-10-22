# Live market data is an installation resource

Accepted under the owner's direction in #3388, including option prices: a compatible live subscription belongs to the installation, not a bot, account, execution mode or worker. IBKR remains the read-only provider; Alpaca remains the execution provider. A qualified contract ID, exchange, currency, security type, requested data kind and entitlement semantics identify a source. A ticker string alone cannot identify an option or a price stream. The installed IBKR library aliases ticker objects by contract ID; a simultaneous incompatible venue for that ID is explicitly refused, never silently substituted.

A separate read-only process owns the upstream subscriptions and reconnects. It has no Clerk volume, Alpaca credentials, orders or custody authority. Workers retain their separate read-only IBKR connections and distinct client IDs for historical reads and contract discovery. Shared delivery carries only market observations. This narrows ADR 0062's process isolation: workers share a market-data dependency, as they already share Gateway, while custody, execution, fencing, credentials and configuration remain isolated. Hosting the source inside one account's worker was rejected because stopping or repairing that account would then interrupt every other account.

One extended-hours five-second TRADES source serves compatible bar consumers; each consumer filters scheduled sessions through the canonical calendar and owns its existing minute/strategy aggregation and continuity proof. A coarser bar never supplies a finer one. A fixed quote tick superset per security type lets option windows, held contracts, chains and status readers reuse the same line. Live, delayed and frozen observations retain their provider labels; only existing submission policy can admit them.

Delivery is bounded per reader. A slow reader loses its own continuity explicitly, without blocking peers. A lost source terminates its generation for all readers; reconnect does not preserve quote readiness. New bar readers start at the source's present position and cannot replay historical entries. New quote readers wait for current price callbacks; no cached tick replay reconstructs a book across destructive size or data-mode transitions. Joining and heartbeats never renew evidence. The last reader releases its source. Original quote-side, halt and trade receipts remain authoritative for freshness.

The internal transport uses the installed HTTP stack and a deployment-owned environment-only token. Redis pub/sub was considered but rejected: it would still require a new demand/lease protocol and would add a second lifecycle to the existing HTTP deployment. No new dependency is introduced. This decision authorizes implementation; it does not authorize changing or restarting a running live bot. Rollout is a separate operator procedure.

Connection health, expected waiting and usable evidence remain separate.
Waiting for the scheduled session or initial callback is a presentation fact;
it does not authorize joining a delivering run. The existing delivering warmup
fact requires an actual recent source observation. Both retain the closed-bar
order gate. Initial source silence during a scheduled session has a bounded
wait; established delivery going quiet remains a fault. This distinction avoids
turning a correction to the operator's explanation into a trading permission.
