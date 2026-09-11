# Alpaca Clerk disposable paper clean slate

During active development, the sanctioned way to restart a disposable Alpaca
paper account is **clean-slate + regenerate**. Do not build an import or
migration path for throwaway paper authority data: it carries stale custody,
catalog, and idempotency history into a run that should instead be generated
again from its current bot configuration.

Use the [Paper developer reset procedure](alpaca-sqlite-clerk-recovery-and-cutover.md#disposable-paper-developer-reset)
for the command, stop boundary, retained evidence and retry behavior. Run it in a
one-shot service container on the same local filesystem as the Clerk volume.

The reset quarantines the target Paper account's SQLite authority and canonical
Alpaca bot directories, and removes its associated saved Paper configuration and
nickname. Live profiles, other accounts, local owner, immutable audit history and
legacy IBKR catalogs survive. Target-local activation evidence establishes the
account mode; Live or Shadow targets and unreadable evidence refuse before moves.
Recreating Paper requires fresh configuration verification, Apply and authority
activation. Repeating a completed reset resumes the same receipt idempotently.

This developer shortcut is not the supervised live-account cutover or reset
ceremony. That production workflow remains evidence-gated. The corresponding
ADR-0035 clarification is deferred to issue #1416's acceptance PR because
ADR 0035 is frozen and must not be edited here.
