# Sanitized Alpaca transaction-history fixture

Source: Alpaca paper-account captures under `tests/fixtures/alpaca/orders/`,
`trade_updates/`, and `activities/`, captured by
`scripts/hitl_alpaca_capture.py` on 2026-07-24.

The Clerk journal envelope is generated from those captures by the normal
Alpaca Clerk path. Account, intent, order, execution, and activity identifiers
are sanitized opaque tokens. Regenerate from a fresh sanitized paper capture;
do not hand-edit to satisfy a projection assertion.
