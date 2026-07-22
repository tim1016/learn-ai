# Account Clerk callback cursors

**Status:** Current supporting technical note (2026-07-22).
**Implementation authority:** ADR-0030 and `docs/architecture/engine-authority-map.md`.
**Operator authority:** `docs/bot-control-operator-manual.md`; this document is for
engineers maintaining callback delivery and must not be used as an operator procedure.

Issue #1045 replaces the transient namespace queue with a non-destructive,
at-least-once read of the Account Clerk journal. Each run persists
`account_clerk_event_cursor.json` only after its own
`broker_callbacks.jsonl` durable write has completed. A crash between those
writes deliberately redelivers the journal row; the callback WAL recognizes
the original journal sequence and does not reapply its fill effect.

| Requirement | Executable evidence |
| --- | --- |
| Ordered journal drain over the production Unix socket | `test_account_clerk_cursors.py::test_drain_events_real_unix_socket_returns_ordered_journal_rows` |
| Account, namespace, and run identity fence | `test_account_clerk_cursors.py::test_drain_events_rejects_stale_account_namespace_and_run` |
| At-least-once recovery, durable-write-before-cursor, and fill idempotence | `test_account_clerk_cursors.py::test_crash_after_bot_wal_before_cursor_redelivers_without_duplicate_fill_effect` |
| Empty reads do not mutate durable state; no inactive namespace relay cache | `test_account_clerk_cursors.py::test_empty_drain_does_not_mutate_cursor_or_journal_or_cache_consumers` |
