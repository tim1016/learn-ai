# ADR 0034: Immutable strategy instances and append-only runs

**Status:** Accepted

- **Date:** 2026-08-02
- **Context:** Alpaca Bot Control safety and reliability remediation PRD, Slice 2
- **Amends:** ADR 0004

## Decision

A strategy instance and a run are different durable identities. Strategy
configuration is persisted once and cannot be changed under the same
`strategy_instance_id`. Each launch persists a separate create-once run record,
and a small replaceable current-run binding identifies the newest run of the
instance.

The artifact layout is:

- `live_state/<strategy_instance_id>/strategy_instance.json` for immutable
  broker-tagged configuration and its configuration hash;
- `live_state/<strategy_instance_id>/runs/<run_id>.json` for append-only launch
  evidence;
- `live_state/<strategy_instance_id>/run_outcomes/<run_id>.json` for the first
  proven terminal outcome of that run; and
- `live_state/<strategy_instance_id>/current_run.json` for the current binding.

The repository composes these records into the existing runner-facing binding
type. Callers therefore do not combine files or independently define instance
and run semantics.

Existing version-1 and version-2 `broker_binding.json` artifacts remain
read-only audit evidence. A read lifts them in memory without rewriting them.
The first later launch deterministically materializes the legacy run and
instance in the normalized layout, preserves the legacy bytes, then appends the
new run.

## Considered options

- **Continue replacing one broker binding:** rejected because it rewrites run
  identity and destroys history while making configuration immutability a
  convention rather than a storage invariant.
- **One append-only mixed event journal:** rejected for this slice because every
  current read would require replay and configuration immutability would be less
  obvious. Event streams may transport projections later without becoming the
  request-time storage model.
- **Persist normalized files while continuing to overwrite the legacy file:**
  rejected because two writable representations could disagree about the
  current run.

## Consequences

Resume can create a new run without changing strategy configuration, and all
prior run identities remain inspectable. Reusing an instance identity with
different configuration or a run identity with different launch evidence is a
typed conflict before the current-run pointer moves.

The current-run binding is not liveness or terminal proof. Process ownership
and lifecycle evidence retain those authorities. Historical-run APIs and UI may
read the append-only records later, but selecting a historical run cannot
retarget a lifecycle command.

## Amendment: bounded run reads and immutable terminal receipts (2026-08-02)

The Python control plane exposes the current run separately from bounded,
cursor-paged previous-run history. Current-run responses may include a fresh
process-registry fact. Historical responses never synthesize process liveness.
Both surfaces return terminal language only when a run-scoped immutable receipt
or the matching current lifecycle record proves it.

Writing the same terminal fact again is an idempotent retry and preserves the
first receipt timestamp. A different terminal kind or reason for the same
`run_id` is a conflict. Request-time reads enumerate only the small per-instance
run directory and return at most 25 records; they never replay the account or
bot journals. A history cursor controls viewing only and cannot change the
current binding or any command target.
