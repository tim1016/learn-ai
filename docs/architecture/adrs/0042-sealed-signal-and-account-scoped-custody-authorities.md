# ADR 0042: Sealed signal decisions meet account-scoped custody at one semantic seam

**Status:** Accepted

- **Date:** 2026-08-21
- **Context:** Sealed Signal Programs to Governed Alpaca Bots PRD, implementation tracker #1723.
- **Amends:** ADR 0034's Dry Run wording; preserves ADRs 0035, 0037, and 0038.
- **Vocabulary:** `CONTEXT.md` — Signal Program, Signal Session, Evaluation Stage, Evaluation Trace, Synthetic Account Authority, and Sealed Account.

## Decision

Python Signal Programs are broker-neutral mathematical authorities. A
registry-selected Signal Program opens a run-local Signal Session that accepts
ordered bars, owns readiness, timeframe consolidation, decision clocks, and
staged candidate state, and emits an Evaluation Trace plus a semantic Action
Plan request. It never selects an account, submits an order, or claims
custody.

The activated account-scoped SQLite Clerk remains the sole custody and effect
authority after that seam. A Paper request uses exactly the sealed real-paper
account. A Dry Run uses an explicitly activated, isolated `sim:` Synthetic
Account Authority and synthetic ports. The two authorities must be selected by
exact account identity and must reject a port or aggregate from the other
account world. No request may silently fall back from a sealed account to a
process-global default.

Each staged candidate receives a Clerk disposition before the Signal Session
advances again. `COMMIT` permits the next mode-specific action after the Clerk
accepts custody; `DISCARD` retains no broker effect and restores any retryable
signal-cycle state. A rejected or unprovable custody result is an explicit
disposition, not a signal-side inference that exposure is flat.

Alpaca Paper exposure carryover is globally disabled. No configuration flag,
environment setting, registry constructor option, or current strategy may
enable it. A later ADR and per-program qualification may introduce a narrowly
scoped allowlist only after replay, retained open-cycle, exit re-emission, and
first-future-decision evidence prove it safe.

Future real-money Live remains unreachable. This decision introduces no Live
mode, no new broker route, and no work on deprecated IBKR bot-control surfaces.

## Consequences

- Backtest, Dry Run, and Paper can share one mathematical decision contract
  without sharing an execution authority.
- Account identity and authority kind become closed cross-boundary facts rather
  than UI- or process-derived assumptions.
- A bot with attributed exposure or a nonterminal ENTER cannot create a second
  exposure operation; an EXIT contention returns the existing custody outcome.
- Operator projections render backend-authored admission and custody results;
  they do not infer safety from strategy state, timestamps, or file fallback.

## Considered and rejected

- **A runner-specific strategy dispatch and simulator journal as the main
  runtime authority:** rejected because it would make Dry Run and Paper diverge
  before custody and leave no common replayable decision boundary.
- **One process-global Clerk for real and simulated runs:** rejected because a
  synthetic health or custody fact could contaminate real-paper authority.
- **A globally configurable carryover switch:** rejected because it grants
  restart exposure semantics without proving them for the specific program.
