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

The offset cursor is an incremental rendering cursor, not a consistency
snapshot. A fleet mutation between page requests can change a later page; the
next fresh page remains authoritative for its own `observed_at_ms`.
