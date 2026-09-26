# Paper/Live decision evidence (#2371)

The specification is [issue #2371](https://github.com/tim1016/learn-ai/issues/2371),
requirements 5–7. This is an internal comparison rule, not a mathematical port
from an external engine. Counts and identities use exact equality; no floating
point tolerance applies.

## Current implementation

`PythonDataService/app/services/paper_live_comparison.py` joins captured Paper and
Live decisions on `decision_bar_close_ms`. Receipt arrival times and local
sequence numbers never align the lanes. A uniquely paired bar with known run
identities and equal, non-null trace digests matches. Different digests diverge.
One-sided rows remain visible. Missing clocks, missing identities/digests,
duplicate bars, revised decision identities/traces and clocks outside a scheduled
regular session are unverifiable. Session anchors come from the canonical NYSE
calendar, including early closes and DST.

Outcome and reason equality are separate fields. Identical decisions can have
different execution outcomes, including an arming refusal on Live. Conversely,
two `no_action` outcomes with different traces are a decision divergence.
`all_decisions_match` requires non-empty, complete captured evidence with every
row matching. It does **not** prove equal deployment configuration, completed
ENTER/EXIT paths, fill parity, current feed health, or authorization to trade.

`PythonDataService/app/services/paper_live_evidence_store.py` archives observations
in `paper-live-experiments/evidence.db` under a caller-supplied control volume.
This database is separate from the generic fleet registry and each Clerk's
custody database. Creating an evidence pair freezes both clerk IDs, account IDs,
assignment generations, database identity tokens and strategy instance IDs.
Every observation must match that provenance.

The Clerk allocates per-bot sequences consecutively from 1 and prunes old ordinary
receipts. The archive retains every collected sequence. Coverage is the source's
highest allocated sequence minus the number of distinct archived receipts. A
missing prefix or interior gap prevents a complete-evidence claim, even when
both lanes have matching surviving tails. Completeness refers only to observed
source watermarks; both capture times remain in the report. It cannot establish
that either bot evaluated every scheduled bar.

The Clerk can revise a receipt's final outcome in place. The archive keeps each
changed observation, refreshes the current projection, and permanently marks
changes to established decision identity/clock/trace as conflicts. Reverting
such a change cannot erase the conflict. Repeated identical observations are
idempotent. Older captures and backwards sequence watermarks are refused.
Receipts, revisions, watermarks and per-session summaries (including both lanes'
run IDs) commit together under SQLite WAL with full synchronization. A failure
to build the comparison rolls the entire capture back.

## Validation

- `tests/services/test_paper_live_comparison.py`: exact join/count fixtures,
  synthetic 26-bar no-action session, prior-session startup evidence, digest and
  outcome differences, missing/duplicate evidence, integer timestamps and early
  close handling. The synthetic session is not a replay or certification of the
  real 2026-09-23 experiment described in the issue.
- `tests/services/test_paper_live_evidence_store.py`: restart durability,
  independent concurrent connections, retention gaps, mutable final outcomes,
  sticky trace conflicts, frozen provenance, idempotency and transactional
  rollback of both evidence and summaries.

## Remaining integration

This is the tested evidence foundation; it has no production collector, HTTP
route, or UI caller yet. Issue #2371 remains open. The next steps are:

1. Read complete, identity-verified Clerk snapshots through the existing fleet
   routing boundary, including revised earlier receipts and explicit sequence
   watermarks. Serialize collection per lane and expose collection failures and
   freshness. Do not infer completeness from the panel's bounded receipt tail.
2. Persist the twin deployment request and each lane's idempotent command state
   before dispatch. Reuse existing admission and deployment commands; preserve
   partial success and unknown outcomes. Both lanes must receive one resolved
   strategy configuration. Creating an evidence pair itself grants no authority.
3. Add the account-desk experiment flow and comparison page. Reuse the existing
   live arming plan/review/typed-confirmation/apply ceremony and per-session
   re-arming. Neither comparison nor deployment may bypass its envelope.
4. Capture causally linked orders and effective fills from their existing Clerk
   authorities, including unknown fees, and add Python-authored differences.
   Paper fill prices are informational; trace equivalence is the decision test.
5. Implement session conclusion and operator-visible collection status, then
   validate a supervised experiment that exercises ENTER and EXIT on both lanes.
   No real-money experiment has been executed by these tests.

Prerequisites #2274 and #2275 are closed. Do not add the temporary launch-time
restriction for the already-fixed warmup bug. IBKR remains the read-only market
data provider and Alpaca remains the account/order/execution provider.
