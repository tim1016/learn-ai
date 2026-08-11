# Null versus verified-zero economics

## Source

Synthetic authority-projection inputs authored for PRD #1441, S0.3.

## Fixture date

2026-08-10.

## Generation and assumptions

`sqlite-cohort-zero-0810` has a complete economic projection and no fills, so
its count and P&L are verified zeros. `sqlite-cohort-unavailable-0810` has no
economic projection for the authority cut, so its economic fields are
unavailable (`null`) rather than zero. These states are intentionally distinct:
zero is a fact; null is an absence of authority.

This is an S2 acceptance oracle. Any change requires a PRD-reviewed
re-derivation; do not replace unavailable values with zero merely to render a
more convenient panel.

## Tolerance

`atol=1e-6, rtol=0` for numeric P&L; count availability and all null/zero
distinctions are exact.
