# Clerk EXIT over a never-accepted ENTRY — lost-submit incident (2026-08-25)

## Source

The 50-bot fleet-stress campaign of 2026-08-25, finding **S15c**
(`docs/audits/bot-fleet-stress-2026-08-25.md`). At 11:44 CT a websocket drop
swallowed the broker response to one bot's ENTRY submit. Alpaca's read-only
`orders:by_client_order_id` endpoint returned 404 for that exact client order
id: the order **never existed at the broker**. The clerk voided the ENTER
(`ORDER_SUBMIT_FAILED_ABSENT`), the bot's subsequent EXIT enumerated the dead
entry, and the EXIT's cancel-prove step folded `ORDER_CANCEL_UNCERTAIN` on
every reconciliation pass thereafter — a permanent `ORDER_OUTCOME_UNKNOWN`
episode that, through the account-scoped outstanding-intents gate, refused
every resume and deploy on the account.

## Sanitization

This fixture records the incident's **ledger shape**, not its data. Every
account, bot, run, decision and order identity from the live account is
replaced by the SQLite Clerk suite's synthetic test identities; no account
number, order id, credential or price from the live account appears here.
What is preserved is the ordered sequence of transition kinds, summary codes
and operation states that put the EXIT into its frozen state — the part the
regression depends on.

## Methodology

`entry_order_ledger` is the ordered custody-transition shape of the ENTRY
order, from ENTER accept through the EXIT accept that enumerated it. It ends
at the EXIT accept because that is the incident's frozen *precondition*: what
one reconciliation pass then does with it is the behavior under test, not
fixture data.

`entry_broker_order_id` is `null` — the load-bearing fact of the whole
finding. The order carries no broker identity, so absence at the exact
client-order-id lookup is definitive rather than a lost response.

`pre_fix_loop_transition_kind` records what `resolve_exit` appended from this
state before the fix, on every pass, forever. It is the shape the regression
must no longer produce.

## Regeneration

The fixture is immutable source data derived from a live incident that will
not recur on demand. It is not machine-regenerated. To revise it, re-read the
S15c narrative and the producers it names
(`order_evidence.py::resolve_order_submission`,
`exit_resolution.py::_cancel_and_prove_entry`) and hand-author the corrected
shape. `test_exit_lost_submit_incident.py::test_the_committed_incident_ledger_
shape_is_reproduced_by_the_clerk` fails if the clerk's real API no longer
produces this shape, which is the signal that the fixture — not the clerk —
needs revisiting.
