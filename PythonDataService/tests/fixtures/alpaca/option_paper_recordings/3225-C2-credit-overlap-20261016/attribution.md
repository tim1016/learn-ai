# Recordings attribution: option orders on Alpaca Paper

- **broker:** alpaca (paper)
- **window:** 1792169383683 up to 1792169917088 ms UTC
- **source:** the capture journal of a live Alpaca Paper account, taken by `scripts/option_qualification_bundle.py bundle` and sanitised by `recordings`
- **sanitization:** `app.broker.capture.sanitize`, the rules `scripts/hitl_alpaca_capture.py` applies: UUIDs replaced with sentinel values, the account number and the account's id replaced with `PA0SANITIZED00001`, client order ids and order refs replaced with one fixture token, secret-like keys redacted, authorization acknowledgements replaced with a structural placeholder. Times, statuses, symbols and numbers are as recorded.
- **account:** not stated here by design

## Lines by family

- `account.jsonl`: 17
- `activities.jsonl`: 16
- `option_contracts.jsonl`: 3
- `orders.jsonl`: 19
- `positions.jsonl`: 21
- `stream.jsonl`: 12

## Repeated polls dropped

A poll, or a page-through of up to eight polls, that asked the same and got the same status and body as the polls kept just before it is not written. The lines above are where an answer changed, not how often it was asked.

- `account.jsonl`: 30
- `activities.jsonl`: 160
- `option_contracts.jsonl`: 4
- `orders.jsonl`: 70
- `positions.jsonl`: 89

## Families left out

Chosen when the recording was made, so their absence is not a gap in the journal.

- `clock`

## Not sanitised, and left out

A line whose body is opaque text, a bare JSON string or not UTF-8 is not written: only JSON can have its keys told, so a secret in anything else cannot be found.

- None.

## Journal evidence the source bundle could not read

- None.

## Journal files the source bundle found absent

- `portfolio_history/2026-10-16.jsonl`
- `assets/2026-10-16.jsonl`

Held beside the documented examples in `tests/fixtures/alpaca_documented/`, never in place of them.
