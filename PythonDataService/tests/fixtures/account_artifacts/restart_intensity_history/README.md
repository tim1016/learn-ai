# Saved account artifacts from the retired account-level restart-intensity gate

Issue #2558 deleted the account-level restart-intensity gate. Account
directories on disk still hold what it wrote, so these files pin that they
keep loading. Nothing here is math; there is no tolerance.

Two account roots, each laid out as `accounts/DU123456/...`:

- `active_breach/` - the gate has just frozen the account: an active
  `unresolved_exposure.flag`, the data-plane producer log (breach event, then
  freeze-recorded event) and one pre-split `account_events.jsonl` row.
- `cleared/` - an operator has cleared that freeze with a clean recovery proof:
  the cleared freeze, both clearance files (`account_recovery_clearance.json`
  and the gate's own `account_restart_intensity_clearance.json`) and the full
  producer log.

## Provenance

Generated once on `master` at 8e138f73, before the gate was deleted, by running
`evaluate_restart_intensity` (policy `threshold=3, window_ms=60_000`) over three
`ACTIVE` bindings of one bot (`spy-1`) at `now_ms=1_700_000_020_001`, then
`clear_account_freeze` with a clean `reconcile` recovery proof recorded at
`1_700_000_030_000`. The files are the bytes the code wrote; only the
`.lock` files and the unrelated binding registry were left out. The producer
log's `recorded_at_ms` is wall-clock time from the capture run.

The `account_events.jsonl` row is the exception: no current writer produces that
file (it is immutable pre-split evidence). It is the breach payload from the
producer log plus `seq=1` and `ts_ms=1_700_000_020_001`, in the pre-split
writer's row shape (sorted keys, compact separators; see
`AccountEventRecord` and the writer at `d953aa0c^`).

Do not hand-edit these files. If they must change, regenerate from a checkout
that still carries the gate and say why in the commit message.
