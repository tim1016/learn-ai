# Clerk execution-coverage set proof — one-to-one

## Source

Synthetic immutable exact and cumulative coverage observations authored for
PRD #1543, stories 18, 19, 28, and 33.

## Methodology

The canonical predicate is
`sqlite/execution_coverage.py::prove_execution_coverage_set`. It accepts only
strict quantity equality within `1e-9` shares and inclusive gross-cost equality
within `max(Q_E, Q_R) × 1e-9` currency units. Exact fees are retained but are
excluded from the proof arithmetic.

## Independent numerical oracle

This is a `hand_computed` tiny synthetic case. Both sides are one observation:
`Q = 2.5`, `C = 2.5 × 101.25 = 253.125`, and `P = C / Q = 101.25`.
The expected values were derived directly from the PRD formula, independently
of the implementation.

## Regeneration

The fixture is immutable source data. To revise it, create a new named case
from the PRD formula, recompute the expected values independently, and update
`test_execution_coverage_set_proof.py` with the case-specific tolerance proof.
