# Alpaca SQLite Clerk execution-lease heartbeat cadence

**Status:** Internal engineering constant (safety margin), not ported reference math.

## Claim

The active SQLite Clerk renews its execution lease **three times per TTL**:

```
heartbeat_interval_s = lease_ttl_ms / (_LEASE_HEARTBEATS_PER_TTL * 1000)
                     = 30_000 / (3 * 1000)   # default 30s TTL
                     = 10.0 s
```

Canonical implementation:
`PythonDataService/app/broker/alpaca/clerk/sqlite/reconciliation_sweep.py`
(`_LEASE_HEARTBEATS_PER_TTL = 3`).

## Why 3× and not a golden fixture

This value is **not transcribed from a reference implementation, paper, or
vendor spec**, so it is out of scope for the `numerical-rigor.md` golden-fixture
regime (which governs numbers ported from a reference and compared for strict
equivalence). It is an availability safety margin, chosen so that a single
missed renewal — a transient disk stall, a scheduler delay, a slow
`asyncio.to_thread` handoff — still leaves roughly two-thirds of the TTL before
the lease expires and writes fail closed. One renewal per TTL would make any
single delayed heartbeat fatal; a much higher multiple would add write pressure
for no additional safety.

Fail-closed direction: if the heartbeat cannot renew (lease genuinely lost), the
writer stops writing rather than resurrecting a stale handle. See
`ClerkSqliteRepository._renew_execution_lease` (strict compare-and-swap on owner
and expiry).

## Validated against

- `PythonDataService/tests/broker/alpaca/clerk/sqlite/test_reconcile.py::test_started_sweep_renews_the_execution_lease_while_idle`
  pins the first heartbeat delay to `0.03 s` (a 90 ms TTL → 0.03 s at 3×) with a
  fixed absolute tolerance, so a regression to an unsafe (larger) interval fails.

## Related

- `docs/references/alpaca-sqlite-clerk-recovery-language.md`
- `docs/references/alpaca-sqlite-clerk-source-guarantees.md`
