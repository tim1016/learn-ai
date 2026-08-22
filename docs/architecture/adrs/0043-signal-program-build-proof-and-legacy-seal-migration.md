# ADR 0043: Signal Program build proof, two-level seal identity, and append-or-clone legacy migration

**Status:** Accepted

- **Date:** 2026-08-21
- **Context:** Sealed Signal Programs to Governed Alpaca Bots PRD
  (`docs/prds/sealed-signal-program-to-governed-alpaca-bot.md`), implementation
  tracker #1723, issue #1728 (PRD Slice 2).
- **Amends:** ADR 0034 (the append-or-clone rule below extends its immutable-
  instance / append-only-run model to the v2 seal); ADR 0042 (adds the seal's
  internal two-level decomposition, settles the running-build-digest source PRD
  §27 left open, and states the atomic decision/custody capture identity rule
  ADR 0042's fold description assumed but did not pin down).
- **Vocabulary:** `CONTEXT.md` — Configured-signal seal, Bot-configuration seal,
  Program build proof, Program build receipt, Signal-decision digest closure,
  Legacy seal migration.

## Context

ADR 0042 established that a sealed Signal Program is a broker-neutral
mathematical authority and that the account-scoped SQLite Clerk owns custody
after that seam. It left three questions for the implementation slice that
would actually seal a program and prove its running build:

1. What exactly does "sealed" mean as a payload — one hash, or a decomposed
   identity with an internal boundary of its own?
2. PRD §11.4 requires that Start and Resume "resolve the digest of the running
   artifact" and refuse with `PROGRAM_BUILD_UNPROVEN` when it has no matching
   golden-qualification receipt, but PRD §27 left the digest's physical source
   an open decision: "container image digest, repository/build manifest, or
   loaded-file digest set."
3. PRD §11.5 requires that a legacy (pre-seal) strategy instance never has its
   v1 bytes rewritten, but does not specify how an operator recovers an
   instance whose persisted parameters can no longer reconstruct an exact seal.

Building the seal surfaced a fourth, unplanned finding.
`app/engine/strategy/registry.py` derives two registrations
(`ema_crossover_2_bps`, `spy_ema_crossover`) from the canonical
`ema_crossover_signal` registration via `dataclasses.replace()`. `replace()`
shallow-copies every field not explicitly overridden — including a new
`signal_program_contract` / `signal_program_factory` pair added for this slice
— so both derived strategies silently inherited the canonical program's
`golden_trace_root` despite running genuinely different decision math (a
relative basis-point gap vs. an absolute-price gap; a bare
`action_plan_contract="none"` wrapper vs. the canonical program). Because the
qualification receipt manifest is keyed on `strategy_key`, no receipt could
ever match either derived key, so every bot on either strategy would have
received `PROGRAM_BUILD_UNPROVEN` permanently. This is recorded here because it
is the direct reason build proof must key strictly on registration identity,
not on inheritance.

## Decision

### 1. The seal is two levels, not one hash

`app/schemas/signal_program_seal.py` seals a deployed program as two frozen,
`extra="forbid"` Pydantic models, each self-verifying its own hash:

- **`ConfiguredSignalProgramSeal`** (inner) — `program_key`, `program_version`,
  `golden_trace_root`, every resolved parameter as a `(value, unit, origin)`
  triple, `parameters_match_validated_settings`, and the closed data/clock
  contracts (provider, symbol, timeframes, timestamp/bar-semantics literals,
  calendar, RTH, warmup, pause/replay policy). It carries no account, mode, or
  execution plan.
- **`SealedBotProgram`** (outer) — wraps a `configured_signal` plus its own
  `configured_signal_hash`, and adds `broker`, `sealed_account_id`, `mode`,
  `action_plan`, `quantity`, `carryover_policy`, the selected validation
  event/snapshot, and `bot_configuration_hash`. `seal_bot_program()` is the one
  authoring seam; a `model_validator` on the outer model recomputes both hashes
  on every construction and refuses a payload whose stored hash disagrees with
  its content.

Resolution feeding the inner seal's parameters is the exact three-tier
precedence PRD §10.3 requires — registered default, then a per-
`(strategy_key, symbol)` profile when the caller has one, then the operator's
deploy-time override — applied once at deploy time in
`paper_deploy_service.py::resolve_deploy_strategy_params` and then sealed. No
caller currently supplies a profile; the middle tier is real code, exercised
today only as a no-op that resolves straight to the registered default.

The v1 `configuration_hash` is never touched. `BrokerBotBinding.sealed_program`
is `Field(default=None, exclude=True)`, so it is invisible to the v1 hash
payload; the v2 seal instead persists as its own create-once sidecar file
(`sealed_program_v2.json`) beside the existing `strategy_instance.json` and
`runs/<run_id>.json` files that ADR 0034 defined. Writing it twice with
different content is a typed conflict (`SealedProgramConflictError`), not a
silent overwrite.

### 2. The running-build digest is a loaded-file digest set, scoped to the signal-decision import closure

PRD §27's open decision is settled: **loaded-file digest set**, not a container
image digest or a repository-commit-plus-dirty-state proof. Both alternatives
would have required runtime access this process does not have by design
(container/image introspection, or a trustworthy `git` state at runtime) and
neither is scoped to what can actually change a decision — a container digest
covers the whole image; a commit digest covers the whole repository working
tree, including files with no bearing on `EvaluationTrace` math.

`SignalProgramContract.artifact_paths` instead names the **signal-decision
import closure**: the transitive first-party (`app.*`) import closure of the
program's declared root modules, minus a documented exclusion list of files
proven unreachable from a bar's decision math. The boundary is exact because
`EmaCrossoverSignalSession.advance()` (`app/engine/strategy/signal_program.py`)
builds and stores the `EvaluationTrace` from `evaluate_signal_bar()`'s return
value *before* `settle()` ever calls `commit_signal_decision()` — so bytes
reached only through the commit path (fill/sizing/portfolio/commission/Insight
publication) cannot retroactively change a trace that already exists. The
exclusion list, with each file's one-line reason, is
`scripts/run_signal_program_build_qualification.py::_EMA_SIGNAL_DECISION_CLOSURE_EXCLUSIONS`.
`tests/engine/strategy/test_signal_decision_digest_closure.py` recomputes the
closure via AST walk (including deferred, function-local imports) and asserts
`closure − exclusions == artifact_paths` exactly, so a newly introduced import
must be explicitly triaged into one bucket or the other before it can land.

`app/services/signal_program_admission.py::running_artifact_digest` hashes
that named file set's current bytes. `prove_running_program_build` resolves it
at Start/Resume, loads the committed qualification manifest
(`app/data/signal_program_build_receipts.json`), and requires a receipt whose
`(program_key, program_version, golden_trace_root, artifact_digest)` all
match. The manifest is never hand-authored:
`scripts/run_signal_program_build_qualification.py` mints a receipt only after
that program's own golden-trace qualification suite passes as a subprocess, so
"CI failure alone is not an admission control" (PRD §11.4) is concrete — a
behavior change without a program-version/trace-root bump leaves the prior,
now digest-mismatched receipt in place, and `prove_running_program_build`
fails closed.

The admission fact is one of three closed states — `PROVEN`, `UNPROVEN`, or
`NOT_APPLICABLE` (no registered Signal Program for this `strategy_key`) — and
only `UNPROVEN` gates Start/Resume, returning `PROGRAM_BUILD_UNPROVEN` before
any run or effect (`app/services/run_admission.py`). `NOT_APPLICABLE` is not a
weaker proof; it is the honest state for a strategy that has no build-proof
claim to make at all, and it is how `ema_crossover_2_bps` and
`spy_ema_crossover` keep executing on their existing path once their inherited
identity is removed (Decision 4 below).

### 3. Legacy migration is append-if-exact, else clone-with-lineage

PRD §11.5's rule is implemented as a strict two-branch decision, made at Resume
(`bot_resume_admission.py::_resolve_program_build`):

- If the instance already carries a v2 seal, prove its running build normally.
- If not, and its persisted v1 parameters still validate against the
  *currently* registered program contract, reconstruct and append an exact v2
  seal under the same `strategy_instance_id`
  (`signal_program_admission.py::reconstruct_legacy_program_seal`, mirroring
  `build_start_program_seal` exactly). This is the ordinary create-once append
  path from Decision 1 — no new identity, no lineage record.
- If the persisted parameters no longer validate against the current contract,
  no future retry of *this* instance id can fix it. Resume clones a new
  instance id, deterministically derived from the original id
  (`legacy_migration_clone_instance_id` — a content hash of the source id, so
  a repeated Resume attempt can never mint a second clone), and — only on the
  mutating Resume call, never on preview — records create-once lineage
  evidence (`LegacyMigrationLineageRecord`, written once under the *clone's*
  own directory, never under the original's) naming the clone's origin and
  reason. The original instance's directory, v1 bytes, and configuration hash
  are never touched. `PROGRAM_BUILD_UNPROVEN`'s `next_step` names the exact
  clone id to deploy.

### 4. A registration's build-proof identity may never inherit by `dataclasses.replace()`

`ema_crossover_2_bps` and `spy_ema_crossover` explicitly set
`signal_program_contract=None` and `signal_program_factory=None` after their
`replace()` call, with an in-code comment naming this ADR's context. Two
structural guard tests
(`tests/engine/strategy/test_registry_signal_program_identity.py`) make the
*class* of bug — not just this instance — impossible: no two registry keys may
ever share a `SignalProgramContract` object or a
`(program_version, golden_trace_root)` identity, and
`signal_program_contract` / `signal_program_factory` must be set or cleared
together. Promoting either derived strategy to its own sealed,
independently-qualified Signal Program is tracked as issue #1730 and is
explicitly out of scope here.

### 5. Decision receipt and custody effect are captured in one SQLite transaction, keyed by `decision_id = evaluation_id`

`ClerkSqliteRepository._commit_transition_row`
(`app/broker/alpaca/clerk/sqlite/repository.py`) inserts the custody
transition row, applies the fold, advances the control revision, and inserts
the mirror-fence row inside one `BEGIN IMMEDIATE` SQLite transaction. When the
transition is effect-bearing, `append_atomic_decision_receipt_row`
(`sqlite/decision_receipts.py`) runs inside that same transaction, so a
receipt and its custody effect are never observable independently of each
other. `runtime.py::execute_for_instance` raises before any of this if the
caller's `decision_id` does not equal the Signal Program's own
`evaluation_id` — the identity PRD §16 requires holds by construction, not by
convention. A rejection that never reaches custody (stream-health hold,
synthetic-source-bar-unproven, and similar pre-custody refusals) still
durably closes the evaluation: `_append_pre_custody_refusal` writes a
`blocked`-outcome decision receipt before the rejection is returned, tagged
`retention_class: "protected_refusal"`. Accepted effects are tagged
`retention_class: "protected_effect"`. The bounded per-strategy
decision-receipt tail (`MAX_DECISION_RECEIPTS_PER_STRATEGY = 1_000`) compacts
only rows without a `protected_*` retention class, so effect-bearing and
refused evidence is exempt from that compaction. Uncertainty, correction,
validation, and seal evidence are separate custody-transition rows in the
same hash-chained log, which this repository does not compact at all.

## Consequences

- Start and Resume for a registered Signal Program now require live,
  freshly-qualified evidence, not a committed-and-forgotten file. A silent
  behavior drift without a matching qualification run fails closed at the next
  Start/Resume, not at the next PR's CI run.
- `ema_crossover_2_bps` and `spy_ema_crossover` are honestly `NOT_APPLICABLE`
  rather than falsely `PROVEN` or permanently `UNPROVEN`; their existing
  execution path is unaffected, and they carry no claim of build-proof
  equivalence to `ema_crossover_signal` until #1730 gives them their own
  qualification.
- A pre-v2 strategy instance either gains its append-only seal transparently on
  its next Resume, or clones forward with durable, one-directional lineage
  evidence. Its original v1 identity and bytes remain permanently
  inspectable and are never rewritten or deleted.
- A decision and its custody outcome cannot diverge: there is no crash or
  retry window where one is durable and the other is not, and the retained
  evidence set (effect, refusal, and every custody-transition row) survives
  the bounded UI tail's compaction.

## Considered and rejected

- **Container image digest as the build-proof source:** rejected — this
  process has no sanctioned runtime path to introspect its own container
  image, and an image digest would cover unrelated files with no bearing on
  `EvaluationTrace` math, forcing needless requalification on unrelated
  changes.
- **Repository commit hash plus dirty-state proof:** rejected for the same
  reason as the image digest (wrong granularity — the whole working tree, not
  the decision closure) plus a runtime dependency on `git` being present and
  trustworthy in every place this process runs.
- **Hashing the full 22-file module-graph closure instead of the trimmed
  signal-decision closure:** rejected. It also reaches
  `app/engine/execution/*` (commission, order, portfolio, sizing) and
  `app/engine/framework/insight*.py` through `strategy/base.py`, none of which
  `evaluate_signal_bar()` can read before its trace exists — hashing them
  would invalidate every program's receipt on an unrelated commission-model
  edit.
- **Letting a derived registration keep inheriting `signal_program_contract`
  from `dataclasses.replace()`:** rejected — this was the actual defect found
  while building this slice (Decision 4). Every sealed identity must be
  explicitly authored by its own registration, never inherited.
- **A single flat seal hash instead of a two-level configured-signal /
  bot-configuration decomposition:** rejected. PRD §11.1 and §11.2 assign
  materially different consequences to a math-semantic change (any resolved
  parameter, timeframe, or replay rule) versus an execution/account change
  (account, mode, Action Plan, carryover) — collapsing them into one hash
  would make a pure execution-plan edit indistinguishable from a change to the
  underlying signal math.
