# Options workspace retained read fixtures

Captured 2026-10-20 (America/Chicago) from the owner's local application for
[#3395](https://github.com/tim1016/learn-ai/issues/3395) through its read API and
repository. Implementation was developed against master `a3bd3a6f`;
runtime scientific identities remain in the receipts. These are retained application results, not independently
reproduced backtests or golden numerical validation.

`run76.json` and `run69.json` are unchanged JSON reads of
`GET /api/research/options-backtest/runs/{id}`. `golden76.json` comes from
`GET /api/research/golden-option-setups/ogs_dbe79f85399048f4a4decc3c86e1f3ff`;
only the bulky execution-proof payload is omitted (replaced by `{}`). Its
immutable identity, historical evidence and current proof status are retained.
`study-record.json` is the retained row read by the existing study repository
for `os_d7b4752086147e437a3a0a56ecc980f33d3c6055`, including frozen protocol,
development/forward projections and snapshot inventory. No final test was opened.

These fixtures test identity and display projections. Canonical numerical
calculations remain covered by their existing engine fixtures/tests. No vendor
capture, replay, approval or configuration command was issued to capture them.
Regeneration requires the same source records and explicitly reviewing any
changed evidence. `capture.py` writes a new capture to a chosen directory;
it never overwrites these checked-in fixtures implicitly.
