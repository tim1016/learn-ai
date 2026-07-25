# Clerk transaction projection backup and rebuild

The Account Clerk JSONL journals are the acknowledgement authority. PostgreSQL
transaction/event/cursor tables are derived evidence only. This runbook repairs
the projection; it never edits, truncates, or recreates a canonical journal.

## Budgets and safety contract

| Operation | Enforced budget |
| --- | --- |
| Normal history page | `1..100` opaque-keyset summaries; no receipts in the grid response |
| Initial Account Desk render | 25 summaries |
| One replay tail | At most 500 complete records and 1 MiB |
| One recovery/rebuild invocation | At most 64 replay tails (resume through the approved job runner) |
| 100k corpus | Peak Python replay allocation ≤16 MiB; catch-up <60 s in the slow benchmark environment |

The response target for a first render is ≤64 KiB and p95 <200 ms after the
database has warmed. Those are deployment SLOs, not permissions to scan the
journal: if they are missed, inspect the query plan/indexes and the projection
state rather than increasing page sizes or adding a fallback scan.

`lag_records` is exact when it is zero. A non-zero value with
`lag_is_lower_bound=true` means the bounded replay knows there is more work but
intentionally has not scanned far enough to count it. `rebuilding`,
`reconnecting`, `corrupt`, and `projection_unavailable` are backend-authored
states. The Account Desk must render them; it must not infer health itself.

## Before a rebuild

1. Identify the account and canonical journal path:
   `artifacts/accounts/<account_id>/clerk_journal.jsonl` for IBKR, or
   `artifacts/accounts/alpaca/<account_id>/order_journal.jsonl` for Alpaca.
   Confirm the account ID from the journal before touching Postgres.
2. Preserve the canonical evidence outside the container. Record a SHA-256 and
   copy the journal with metadata. Treat a hash mismatch as a stop condition.
3. Take a restorable database backup before altering derived data. For example,
   run `pg_dump` for `clerk_transactions`, `clerk_transaction_events`,
   `clerk_transaction_projection_cursors`, and
   `clerk_transaction_feed_status` (or take the normal full database backup).
4. Check the transaction endpoint. If it says `corrupt`, retain its
   high-water/detail receipt with the incident record. Do not request a broker
   sweep to compensate for the projection.

## Rebuild procedure

1. Run the authenticated maintenance job that calls
   `rebuild_account_transaction_projection(artifacts_root=..., account_id=...,
   broker='ibkr' | 'alpaca', store=PostgresClerkTransactionProjectionStore())`.
   It takes a journal-scoped PostgreSQL advisory lock, deletes only that
   broker's derived rows and matching cursor, marks the feed `rebuilding`, then
   replays the selected canonical Clerk journal. It has no broker client and
   never writes the journal.
2. If it returns `rebuilding`, resume it through the approved job runner with
   `reset=False` until the API reports `live`. A completion reports zero lag
   and a high-water journal sequence. Do not substitute direct SQL deletion;
   the service owns the status receipt and lock.
3. Compare the resulting high-water and transaction/event counts with the
   pre-rebuild backup. For any discrepancy, compare canonical receipt IDs and
   journal sequence—not broker history. The fault-injection tests establish
   that a failed write or crash replays idempotently without changing canonical
   acknowledgement evidence.
4. Keep the database backup and canonical hash with the operator incident.
   Rollback restores only the derived database backup; it never restores over a
   newer canonical journal.

## Corruption and crash handling

- A trailing unterminated JSONL row is left untouched and retried when its
  writer completes it.
- A malformed complete row, cursor beyond the current file size, or a record
  above the 1 MiB replay limit marks the projection `corrupt` and stops. Keep
  the bytes, capture the hash, and escalate the canonical artifact incident.
- A database failure before commit leaves the cursor unchanged; the next
  attempt replays the same canonical bytes. A crash after commit may publish a
  duplicate notification, but database event identity deduplicates the
  semantic event.
- Never repair a journal by deleting a row, rewriting a sequence, deduplicating
  an opaque callback in place, offset-paginating history, or falling back to
  Account Truth/broker reads.

## Verification

Run the focused replay suite plus the explicit scale corpus in a configured
Python service environment:

```sh
python -m pytest tests/services/test_clerk_transaction_projection.py tests/routers/test_clerk_transactions.py -q
python -m pytest tests/services/test_clerk_transaction_projection.py -m slow -q
```

The second command creates a deterministic 100,000-record canonical-journal
corpus, verifies bounded replay tails, final keyset-ready high-water state, and
the replay allocation/time budgets. It is intentionally marked slow so normal
developer feedback remains fast.
