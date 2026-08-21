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

## Amendment: evidence-only human overrides (2026-08-20)

An operator may deploy a human-validated strategy whose behavioral-equivalence
verdict is `evidence_only` only through the closed Alpaca paper override
contract. The request must carry the exact acknowledgement
`I_ACCEPT_EVIDENCE_ONLY_DEPLOYMENT_RISK` and a substantive operator reason.
Both values are part of immutable strategy-instance configuration, its
configuration hash, every resumed run binding, and the terminal deployment
receipt.

This is not a general admission bypass. The override cannot substitute for a
human validated flag, cannot accept an invalidated or rejected event, and
cannot register missing executable strategy machinery. Account posture, Clerk
custody, channel health, intent custody, market data, and Start admission
remain fail-closed. Changing the override decision or reason requires a new
`strategy_instance_id`; Resume preserves the original decision.

## Amendment: mode-tiered admission (2026-08-20)

The evidence-only override amendment above states plainly, one section up,
that Paper deployment of a `evidence_only`-verdict strategy is possible
*only* through the closed override contract. That sentence was applied at
the wrong tier. It is corrected here as a recorded decision — the PRD's
"Decisions confirmed — 2026-08-20 (operator review)" section (#1697,
decision 3) already resolved this; this amendment transcribes it.

Every admission gate splits into exactly one of two kinds:

- **Custody gates** — is the account provably sound (Clerk reconciliation,
  freeze, exposure hold, intent custody, account tradable posture, the
  execution channel). These never relax. They protect the account, not the
  math, and no execution-mode tier changes them for `paper` or `trade`.
- **Evidence gates** — how confident are we in the math (the human-validated
  flag, the accepted behavioral verdict, the evidence-only override). These
  relax by tier, because they answer "should this specific strategy's logic
  be trusted with this consequence," and Dry Run and Paper carry different
  consequences.

Three tiers, tiered by what each one is worth:

- **Dry Run** requires only a registered runtime and a healthy market-data
  channel. It ignores the human-validated flag, the behavioral verdict,
  account tradable posture, Clerk custody freeze, exposure hold, and intent
  custody — Dry Run makes no broker-execution contact, holds no custody, and
  records only through its own journal, so none of those facts describe a
  risk Dry Run carries. Exposure carryover is forbidden outright.
- **Paper** requires the human-validated flag and full Clerk custody proof
  (reconciliation, freeze, hold, intent custody, account posture, both
  submission channels). It no longer requires a risk acknowledgement or the
  evidence-only override for a human-validated strategy, regardless of
  whether its behavioral verdict is `accepted` or `evidence_only` — the
  verdict still displays, but only the validated flag plus full custody
  proof gate Paper now. Custody gates are untouched.
- **Live** (planned, still unreachable) additionally requires the accepted
  behavioral verdict, or — for a human-validated strategy whose verdict is
  not accepted — the closed evidence-only override contract from the prior
  amendment. This is exactly where that contract now applies. An operator
  will need it only when Live exists and only for that one case.

Nothing already built is discarded. The override schema
(`AlpacaPaperEvidenceOverride`), its receipt plumbing (the override decision
and reason preserved on the terminal receipt when present), and the
immutable strategy-instance configuration hashing all survive unchanged in
shape. Only the Paper-gating call site is removed: a `paper`-mode deploy
request that still carries an `evidence_override` is refused with a typed
conflict naming Live as the contract's tier, the same "surface invalid
input, never silently drop it" pattern the accepted-strategy override
rejection already used. The Live branch of the gate table is written and
stubbed — `execution_mode` stays closed to `paper` and `dry_run` — so no
admission, routing, or execution path for Live opens as part of this
amendment.

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
