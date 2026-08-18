# ADR 0037: SQLite is the sole Alpaca custody authority; legacy JSONL is retired

**Status:** Accepted

- **Date:** 2026-08-17
- **Context:** Wayfinder map [#1588](https://github.com/tim1016/learn-ai/issues/1588),
  decision ticket [#1596](https://github.com/tim1016/learn-ai/issues/1596); the
  reachability audit in `docs/audits/clerk-lineage-reachability-2026-08-17.md`.
  Grilling session: `grill-with-docs` + `domain-modeling`, 2026-08-17.
- **Completes:** ADR 0035, which accepted SQLite as the Alpaca authority for an
  activated account but left the un-activated case selecting legacy JSONL.
- **Vocabulary:** `CONTEXT.md` § "Custody authority (resolved 2026-08-17)".

## Decision

**1. An Alpaca account has one custody authority: the activated SQLite one, or none.**

`select_active_clerk_runtime` currently treats a *missing* activation record as a
silent instruction to construct the legacy JSONL Clerk, while an *invalid* record
fails closed. That is backwards — absence is treated more leniently than
corruption. Absence now fails closed too. There is no legacy fallback and no
second authority to reconcile against.

**2. The IBKR lineage is not, and never becomes, an Alpaca custody authority.**

It remains live for two sanctioned, non-custody roles: the market-data bridge
(ADR 0032, which already confines it to bars and forbids it order effects) and
the global connection banner. Its `engine/live/account_clerk*` modules are IBKR's
own custody machinery and stay that way.

**3. The two shared evidence models stop being shared.**

`AccountClerkPositionEvidence` and `AccountClerkBrokerEvidenceBaseline` remain
where they are, in IBKR's `account_clerk_journal_models.py`. What ends is Alpaca
importing them: their only Alpaca caller is legacy inventory-baseline recovery,
which Decision 1 retires. No parity test is owed, because after this there is no
duplication to pin — the coupling simply ceases.

## Considered and rejected

- **Keep legacy as the un-activated default.** Nothing breaks for un-migrated
  accounts, but two custody authorities stay live indefinitely, "which authority
  is this account on?" remains a question every reader must ask, and the numeric
  census's eight implementations stay at eight.
- **Keep legacy behind an explicit activation record naming it.** Preserves an
  escape hatch and removes the silent default. Rejected because an escape hatch
  nobody is expected to use is still a second authority that must be kept
  correct, tested, and reasoned about — the cost is ongoing, the benefit
  hypothetical.

## Consequences

Not implemented by this ADR. Each needs a regression test that fails before and
passes after, per `CLAUDE.md`.

1. **`active_authority.py`'s `activation is None` branch returns unavailable**,
   with a recovery message naming the cutover workflow. The `legacy_factory`
   parameter and its call sites become dead.

2. **`rollup_cache.py` becomes dead code.** It is reached only via
   `panel_data_source.py`'s `if projection is None` branch, which is the legacy
   path. **This supersedes ADR 0036 consequence 1** — the rollup's wrong flatness
   inclusivity is resolved by deleting the file, not by correcting it. Do not
   spend a fix on code that is about to be removed; see #1606.

3. **The numeric authority census drops from eight backend implementations to
   six.** Its implementation 5 (legacy JSONL account projection/delta in
   `exposure.py`) and implementation 6 (the rollup exposure mirror) both die with
   legacy. `docs/references/clerk-position-drift-tolerance.md` already anticipated
   this: "exposure.py's JSONL-journal implementation is retired at the SQLite
   cutover (#1382), at which point this is the sole implementation."

4. **The legacy branches in `clerk_transactions.py`, `panel_data_source.py`, and
   `broker_v2_panel/*` lose their reachable case** and can be collapsed. Several
   already carry comments asserting SQLite must not construct legacy evidence;
   those assertions become structural rather than defensive.

5. **A migration gate is required before this lands.** Decision 1 makes any
   un-activated account inoperable rather than degraded. Landing it without first
   confirming every account in use carries a valid activation fence would take
   custody offline for that account. The fix must verify this, not assume it.

6. **The global banner is retained, and its label is wrong.** IBKR health genuinely
   affects Alpaca bots — IBKR is their market-data feed under ADR 0032, so a dead
   IBKR connection stops Alpaca signals. Showing it on every route is correct. But
   it reads as *broker connection* when what it reports is *market-data health*,
   which on an Alpaca page invites exactly the wrong inference. This is a labelling
   defect for the register, not a reason to remove the banner.

## Why this was worth an ADR

A future reader will find an account with no activation record refusing to trade
rather than falling back to a working implementation that is still in the tree,
and will be tempted to restore the fallback. The fallback is the thing being
removed, and the reason is that a second custody authority costs more to keep
correct than it ever returns.
