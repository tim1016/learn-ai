# Bots first-paint performance budget

Issue #1225 Slice 7 establishes a render budget for `/broker/bots`.

## Measurement fixture

`test_bot_catalog_page_bounds_initial_status_composition_to_requested_rows`
creates 30 durable bot runs with no daemon-owned process records. This is the
expensive case: after the one fleet snapshot, each displayed card would need a
per-instance daemon/status composition.

| Stage | Before | Budget after this slice | Regression proof |
| --- | ---: | ---: | --- |
| Requests issued before the initial page shell can paint | 1 full catalog request | 0 | `BotsPageComponent` first-paint test |
| Catalog cards composed by the first request | 30 (all fixture rows) | at most 25 | Python page-budget test |
| Fleet daemon snapshot after first paint | 1 | 1 | required to report current, rather than inferred, process state |
| Per-card daemon/status compositions after first paint | 30 | at most 25 | Python page-budget test |

The frontend schedules its first catalog page in a new task after Angular has
rendered the page shell. The page initially states that the catalog and fleet
summary are not requested; it never presents zero counts as a fresh roll-call.
The first page is limited to 25 cards. Operators can explicitly load the next
page, and the UI labels the distinction between loaded matches and the known
catalog count.

## Freshness and unavailable semantics

This is not a cache and does not make broker calls. Every requested page takes
a fresh daemon fleet snapshot and returns `observed_at_ms`. The page deliberately
does not include roll-call or evening-report aggregates, because generating
those values would require full-fleet composition. Until an operator runs roll
call, that summary is shown as not evaluated. If the daemon is unavailable,
the existing status projection reports its unavailable/degraded evidence for
the requested cards rather than reusing stale data.

The cursor is the last strategy-instance ID from a lexicographically ordered
page, not a consistency snapshot. A fleet mutation between page requests can
change the current total and the next fresh page remains authoritative for its
own `observed_at_ms`; keyset pagination does not replay already loaded IDs.
New IDs that sort before the cursor appear on the next full refresh rather than
being inserted into an in-progress browse.

## Alpaca account-scoped fleet signals

The v2 route at `/brokers/alpaca/accounts/:accountId/bots` exposes three User
Timing entries so a repeatable browser trace can distinguish rendering from
broker and catalog latency:

| Entry | Kind | Boundary |
| --- | --- | --- |
| `alpaca-bots-route-shell` | mark | Angular rendered the route shell and loading posture |
| `alpaca-bots-fresh-roster` | measure | catalog request started → fresh roster committed |
| `alpaca-bots-routine-action` | measure | Start/Stop requested → action settled locally |

Account resolution no longer performs independent vendor reads for the route,
account posture, and panel/catalog validation. Those consumers share one
broker-authored observation, coalesce concurrent requests, and retain it for a
60-second server-side window keyed to the registered broker port. The Angular
service also coalesces account reads during route construction. This does not
make a stale observation look fresh: the fleet and posture retain explicit
loading, refreshing, last-good, and unavailable labels.
