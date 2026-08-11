# Downward execution correction

## Source

Synthetic execution evidence and an authority correction authored for PRD
#1441, S0.3. The original evidence is a websocket execution slice; the
subsequent correction models the durable `EXECUTION_CORRECTED` transition
specified for S1.

## Fixture date

2026-08-10.

## Generation and assumptions

The original five-share SPY BUY remains an auditable fill row. A correction
supersedes it with an effective three-share BUY at the same price. The position
therefore changes by `3 - 5 = -2` shares and ends at three shares; it must not
be represented by silently inserting a negative-quantity fill. The mark equals
the corrected price, so open P&L is $0.00.

This is an S1/S2 acceptance oracle. Any change requires a PRD-reviewed
re-derivation; it must preserve both the original evidence and the corrective
link.

## Tolerance

`atol=1e-6, rtol=0` for P&L; counts, effective quantity, identities, and
supersession links are exact.
