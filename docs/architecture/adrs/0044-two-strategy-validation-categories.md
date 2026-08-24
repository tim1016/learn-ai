# ADR 0044: Two strategy-validation categories with a permanent Live ceiling for operational harnesses

**Status:** Accepted

- **Date:** 2026-08-23
- **Amends:** ADR 0020 and ADR 0023 by separating externally validated trading
  strategies from the deployment machinery's internal test strategy.
- **Vocabulary:** `CONTEXT.md` — Production candidate, Operational validation
  harness, Strategy validation proof.

## Context

The validation catalog contains both real trading strategies and
`deployment_validation`, a strategy-shaped program whose purpose is to exercise
deployment and custody plumbing. Requiring a QuantConnect run for the harness
misrepresents what it proves, while treating it like a normal validated strategy
would let a test instrument drift toward Live eligibility.

## Decision

Every catalog-visible strategy belongs to exactly one registry-owned category:

- A **production candidate** follows the external-reference proof cycle: sealed
  program contract, exact QuantConnect audit source, pinned QuantConnect run,
  behavioral reconciliation, human review, and current artifact hashes. A
  current accepted proof can admit Paper; Live remains a separate future release
  decision and gate.
- An **operational validation harness** follows an internal deterministic replay
  and qualification cycle. QuantConnect source and run stages are not
  applicable. It may be admitted to Dry Run or Paper, but it is permanently
  ineligible for Live.

The category is strategy metadata, not an operator choice. Validation presents
the proof as an ordered dossier with the first blocker and recovery references;
stale evidence remains inspectable but cannot become a current accepted proof.

## Consequences

- New real strategies inherit the production-candidate path unless explicitly
  registered as an operational harness.
- Future Live admission must refuse the operational-harness category even when
  every applicable harness proof stage is current.
- Operators do not invent external evidence for an internal deployment test,
  and production candidates cannot bypass external reconciliation by adopting
  the harness category in the UI.
