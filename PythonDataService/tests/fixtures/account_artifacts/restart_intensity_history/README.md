# Saved account artifacts from the retired account-level restart-intensity gate

Issue #2558 deleted the account-level restart-intensity gate. Account
directories on disk still hold what it wrote, so these files pin that they
keep loading. Nothing here is math; there is no tolerance.

Two account roots, each laid out as `accounts/DU123456/...`:

- `active_breach/` - the gate has just frozen the account: an active
  `unresolved_exposure.flag` and the data-plane producer log (the breach event,
  then the freeze-recorded event).
- `cleared/` - an operator has cleared that freeze with a clean recovery proof:
  the cleared freeze, both clearance files (`account_recovery_clearance.json`
  and the gate's own `account_restart_intensity_clearance.json`) and the full
  producer log.

## Provenance

Every file is the bytes the code wrote. Generated once on `master` at 8e138f73,
before the gate was deleted, by running `evaluate_restart_intensity` (policy
`threshold=3, window_ms=60_000`) over three `ACTIVE` bindings of one bot
(`spy-1`) at `now_ms=1_700_000_020_001`, copying the account directory
(`active_breach/`), then `clear_account_freeze` with a clean `reconcile`
recovery proof recorded at `1_700_000_030_000`, and copying it again
(`cleared/`). `.lock` files and the unrelated binding registry were left out.
The producer log's `recorded_at_ms` is wall-clock time from the capture run.

There is deliberately no pre-split `account_events.jsonl` here. The gate
existed before the July 2026 writer split, so an older account may hold a
breach row in that file, but current code cannot write one: a faithful copy
would have to come from running the pre-split writer (`d953aa0c^`), and this
fixture does not. That file's reader only parses JSON rows and never looks at
event types, and the producer-log payloads here already carry the breach
event's `window_*` fields.

Do not hand-edit these files. If they must change, regenerate from a checkout
that still carries the gate and say why in the commit message.
