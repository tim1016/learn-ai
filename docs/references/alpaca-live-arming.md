# Alpaca live arming — the ceremony, the ledger, and the lapse

**Status:** retired design (ADR 0059 Decision 3), superseded on 2026-09-27
by PRD #2540 and #2547. Nothing checks permission against an arming any more
(#2629). The ceremony, the ledger format and the lapse rule are in Git
history.

## What remains (#2629)

The arming gate (`live_arming_gate.py`), its per-tick refresh
(`sqlite/arming_refresh.py`), the envelope seal (`LiveEnvelopeGate.sealed`
and its `LIVE_ENVELOPE_DISAGREEMENT` refusal), the Start-time arming fact
(`live_arming_admission.py`), the status rule (`arming_status`,
`sessions_used`) and the Apply-time arming invalidations (`arming_policy.py`)
are deleted. A version-1 account still refuses every ENTER under
`BUDGETS_NOT_SWITCHED_ON`. What is left only reads history:

- `PythonDataService/app/broker/alpaca/clerk/live_arming.py` — the two record
  shapes, verified exactly as they were sealed, `latest_arming`, and the ENTER
  refusal codes old `blocked` receipts were recorded under
  (`ARMING_ADMISSION_REASON_CODES`, still transient in `sqlite/uncertainty.py`).
- `PythonDataService/app/broker/alpaca/clerk/live_arming_ledger.py` — the
  read-only reader of `<clerk_dir>/accounts/arming/<live_account_id>/live_arming.jsonl`.
- Its one reader: the one-time exit-terms upgrade
  (`SqliteAlpacaClerkFacade.upgrade_legacy_exit_terms`), which prices a bot
  armed before exit terms existed from its own newest arming. The entry
  allowance resolver no longer reads it: a `sim:` Dry Run bound to a live
  account prices its entries from that account's current envelope.
