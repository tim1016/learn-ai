# ADR-0026: Daily bot lifecycle — three durable states, the Button Rule, and the single-writer evaluator

**Status**: Accepted 2026-07-08. Decided in the PRD #974 rev 3
simplification pass (operator direction: "use the human analogy; manage
only states with a known cure; banish the rest as non-remediable;
operate everything from the UI, never the CLI") and its rev 3.1
question round.
**Related:** PRD #974 rev 3.1 (requirements, user stories, testing
seams, slicing — this ADR records the architecture; the PRD stays the
requirements authority), ADR-0010 (poison → redeploy; its routing is
subsumed by the one-start-path decision, its evidence and labels are
retained), ADR-0011 (halt-on-transition), ADR-0019 (daemon diagnostics
— composed here as evidence, not duplicated), ADR-0022 (temporal
authority — every session boundary in this ADR derives from the
canonical calendar), ADR-0025 (single dominant headline — the
attention-badge precedence in §9 reuses its dominance pattern).

**Amendment 2026-07-21 — Account Clerk PRD #1153, Slice 7.** The
single-writer decision in §4 is now implemented by
`bot_lifecycle_evaluator.py`: it is the only durable writer of duty phase,
roster membership, and desired-state control intent. Routers and the CLI
submit commands; the daemon only actuates host processes and reports facts or
terminal outcomes for the evaluator to fold. This amendment supersedes the
historical removal of Pause/Resume in §§3 and 5: `PAUSED`, `RUNNING` (Resume),
and `STOPPED` remain durable control-plane commands, and must work without a
Clerk or broker connection. Start may observe but never clear `STOPPED`, and
may enter `ON_DUTY` only after account admission has granted it.

## Context

Bots have no daily life. A bot process persists overnight, idling,
holding whatever it held; overnight is where everything breaks (lease
loss, recovery-flatten timeout, stranded exposure → account-wide
`watchdog.flatten_timed_out` freeze). Exit-code-only retirement brands
every unexpected end a crash — live evidence: 10 registry
`process_crashed` bots of which only 4 were real (`exception`); 5 were
deliberate `fatal_halt`, 1 wrote no status.

Beyond honesty, the state space itself outgrew the operator. The
2026-07-07/08 redeploy audit
(`docs/audits/bot-control-panel-redeploy-observations.md`, 19
inspections) documents: an open vocabulary (`BLOCKED`, `DEGRADED`,
`Fresh run only`, `durably STOPPED`, `Recovery lane · Poisoned`,
`Frozen`, `Exposure Unknown`, `No result yet`); a resume dead-end
reproduced on three bots (durable STOPPED latch → Resume disabled →
no way forward); a CLI instruction rendered in operator copy
("clear it with 'run.py resume'"); the deploy form asserting
`Account Clean / Fleet Clear` while the detail page showed
`Account frozen` (five occurrences); and a control bar of eight
mostly-disabled buttons.

PRD #974 rev 2 fixed exit honesty with a six-state durable machine,
multi-owner state writes, and per-cause recovery lanes. The rev 3
simplification pass asked whether six states, many writers, and
parallel lanes are necessary. They are not. This ADR records the
resulting architecture.

## Decision

### 1. Exit-and-sleep over persist-overnight

A bot process exists only during its trading session. At stop-time
(calendar-derived `effective_stop(D) = min(configured_stop,
session_close(D))`, per ADR-0022) or on operator "End day now", one
shared clean-exit procedure settles to intended exposure, releases the
session lease, clears the account, and writes a durable
`CleanExitReceipt`. Overnight there is no process and no lease.
"Clean" is proven by the receipt, never asserted.

### 2. Three durable states — phase is presence, health is derived

The durable machine tracks *where the bot is*; *how it is* derives
from evidence. Rev 2 → this ADR: `DORMANT_CLEAN`→`OFF_DUTY`,
`ACTIVE_SESSION`→`ON_DUTY`, `RETIRED`→`RETIRED`;
`WARM_START_READY`→perishable offer artifact; `CLEAN_EXITING`→internal
sub-phase of `ON_DUTY`; `TRIAGE_REQUIRED`→derived `sick` flag.

| State | Meaning | Entry | Exit |
|---|---|---|---|
| `OFF_DUTY` | No process, no lease | Deploy (born asleep); clean-exit completion (receipt `CLEAN` or `FAILED`); evaluator observes a dead run | Confirmed roll-call offer → `ON_DUTY`; Retire → `RETIRED` |
| `ON_DUTY` | Session lease held, `run_id` bound | Start path: re-check → fresh lease → new `run_id` | Clean exit → `OFF_DUTY`; observed process death → `OFF_DUTY` + conditions |
| `RETIRED` | Terminal | Retire action (replacement optional, default on) | none |

Derived, never persisted as phase: `sick` (owns ≥1 open condition,
§8), `ready` (holds an unexpired offer), `on_roster` (operator-owned
boolean — the duty roster; replaces the Rest/Stand-down intent enum).
Warm-start readiness is a **persisted artifact of the roll-call tick**
(`offer_id`, `session_date`, `issued_at_ms`, `expires_at_ms =
effective_stop(D)`, evidence snapshot), not a lifecycle state: offers
expire at stop-time, so the day boundary garbage-collects readiness
and no decay transitions exist.

**Amendment 2026-07-15 — presence is not trading permission.** `ON_DUTY`
means that the bot process is present and bound to a run. It does not assert
that orders may be submitted. Trading permission is the conjunction of the
account proof selected for the deployment (eventually the Account Observation
Lease), the run-scoped reconciliation receipt, and the existing submit chain.
The accepted Start transition therefore remains presence-honest while the
existing 15-second Account Truth observer supplies the coarse background drift
check by renewing or revoking account evidence. Account proof is evidence and
never a second phase writer.

### 3. The Button Rule

**Every non-terminal state and every condition must declare exactly
one primary exit action, delivered as a UI button.** A state or
condition that cannot name its button is not added to the system; it
folds into Retire & Replace (§7). No operator copy may instruct a CLI
step. The UI renders only the actions the current state allows — no
disabled-button graveyard.

Two refinements:

- **One exit, possibly a choice inside it.** When the domain has more
  than one honest fix, the single exit button opens a dialog with a
  closed set of resolutions. An exposure freeze is one condition row
  with one cure (`resolve_exposure`) whose dialog offers
  flatten→clear or accept→audited-override.
- **Ambient controls.** Besides each row's primary exit, a closed set
  of instance-level controls exists, availability a pure function of
  state, rendered only when legal: roster toggle (any non-retired
  bot), End day now (`ON_DUTY` only), Retire (`OFF_DUTY` only).
  Nothing else exists. Intraday pause/resume/flatten-and-pause are
  deleted, not deferred.

The rule is enforced by a schema-level contract test: every emitted
state/condition carries exactly one action id from the closed
`cure_action` set (§8); zero, multiple, or out-of-set actions fail.

### 4. Single-writer evaluator; reads are pure

Only the lifecycle evaluator commits phase transitions; the start,
clean-exit, and retire paths call through it. Everything else —
daemon, engine, triage service, registry — writes **evidence**
(receipts, `run_status`, freeze artifacts, lease records) and never
phase. The evaluator is a pure function
`f(evidence) → (phase, offers, conditions)`.

Persist points are explicit: lifecycle commands (start, clean exit,
retire) and scheduled ticks (roll call at session open; a coarse
background tick that reconciles drift). **Reads never write**: a
surface load runs the evaluator and returns the computed projection —
with a drift flag when it disagrees with the persisted phase — and
persists nothing. Phase is therefore durable (survives restarts) yet a
rebuildable projection of evidence, the repo's files-canonical /
projection pattern. The registry-ACTIVE-but-dead trust leak heals
through this one function instead of an ad-hoc daemon.

### 5. One start path — resume is deleted

Every start — morning confirm, same-day restart after a cure, first
start after deploy — is one path: re-check evidence → acquire fresh
session lease → bind new `run_id` → `ON_DUTY`. The durable-STOPPED
latch, Resume, and the fresh-run-vs-redeploy fork are deleted. The
honest exit taxonomy (consuming `run_status.exit_reason`, never the
exit code) drives **labels and receipts, not routing**: every recovery
converges on "cure the condition, then pass roll call."

Boundary and restart rules: the boundary is strict
(`deploy_instant < effective_stop(D)`; the start path refuses at
`now ≥ effective_stop(D)`). Same-day restarts **never adopt exposure**
in the MVP: the prior run's durable intent WAL classifies any leftover
position (honest provenance in the `resolve_exposure` dialog), but the
leftover is resolved — flattened or accept-overridden — before the new
`run_id` starts flat. Position adoption plus strategy-state
restoration is the swing slice, not smuggled into intraday.

### 6. Run identity

Stable `strategy_instance_id` across days: owns lifecycle phase,
roster membership, config, and the cross-session chart and audit
trail. New `run_id` per trading session: owns that session's logs,
`CleanExitReceipt`, live-state sidecar, intent WAL, `run_status`, and
per-session order ledger. Attendance history derives one cell per
`session_date` from receipts.

### 7. Retire & Replace — death is cheap

Banishing non-remediable states is safe only if the banishment
destination is painless. Retirement is **one action**: it writes a
`RETIRED` binding with the honest exit label and a lineage link
(`replaces`), keeps the instance's history browsable read-only, and —
with the default-on "create replacement" option — pre-fills a fresh
deploy from the retired config, born asleep, on tomorrow's roster.
Unchecking the option is the explicit decommission; there is no second
flow. The crash-recovery-attestation lane is replaced by this action's
confirm dialog (death evidence + explicit reviewed-acknowledgement).
Retire is the universal cure for `crashed`, `ended_without_status`,
and any condition with no defined in-place cure.

### 8. The account is the only shared gate; conditions are derived; the enum is closed

Bots never gate bots. The sick bay is an account-scoped board of
**conditions** — derived projections computed by the evaluator from
durable evidence, never persisted rows. A condition "closes" when the
evidence no longer implies it; cures write evidence (clear proof,
audited override, fresh receipt), nothing flips a condition bit. Rows
dedupe by `(condition_type, owner)`; account-scoped conditions gate
all starts, bot-scoped only their owner. An active freeze renders as
one global banner primitive on every surface from this same
projection; the deploy form has no readiness facts of its own.

**The closed `condition_type` enum (canonical here — the slice-2
gate):**

| `condition_type` | Scope | `cure_action` |
|---|---|---|
| `exposure_freeze` | account | `resolve_exposure` |
| `account_freeze` (non-exposure) | account | `clear_freeze` (guarded) |
| `evidence_stale` (receipt absent or past freshness) | account | `reconcile_now` |
| `daemon_unreachable` | account | `reconcile_now` |
| `evidence_missing` | bot | `prove_evidence` |
| `exit_flatten_failed` | bot | `resolve_exposure` |
| `exit_lease_stuck` | bot | `reconcile_now` |
| `crashed` | bot | `retire_replace` |
| `ended_without_status` | bot | `retire_replace` |
| `repeated_unclean_start` (crash-loop guard) | bot | `retire_replace` |

The closed `cure_action` set: `resolve_exposure`, `clear_freeze`,
`reconcile_now`, `prove_evidence`, `retire_replace`. Extending either
enum requires naming the one exit button (Button Rule, §3) and
amending this ADR. `daemon_unreachable` composes ADR-0019's diagnostic
ladder as evidence; it does not re-derive it.

### 9. Closed operator vocabulary and display precedence

Display states are exactly: **Off duty, Ready, On duty, Clocking out,
Sick bay, Off roster, Retired**, plus one backend-authored reason
line. `DEGRADED`, `BLOCKED`, `Fresh run only`, `durably STOPPED`,
`Poisoned`-as-status, and `Resume` are removed from all surfaces. The
word "Unknown" never renders: missing evidence renders as "Not yet
proven: <evidence> [Prove now]".

Every fleet row shows its presence chip (`Off duty` / `On duty` /
`Retired`) — the chip never lies about presence — plus at most one
attention badge, precedence **Sick bay > Ready > Off roster**
(ADR-0025's single-dominant pattern applied to the fleet row). Sick
counts include off-roster bots; the off-roster count covers only
healthy bots. A retired bot still owning an account-gating condition
stays `Retired` while its condition renders in the sick bay under its
ownership; curing it never resurrects the bot.

## Consequences

**Positive:**
- The operator model fits in a head: three states, one gate, one start
  path, and every rendered screen has exactly one way out. The audit's
  dead-ends (resume loop, CLI instruction, contradictory surfaces)
  become structurally impossible, not individually patched.
- Multi-writer state disagreement is gone by construction; the
  registry-ACTIVE-but-dead leak heals through the evaluator.
- Attestation survives as a moment (Retire's confirm dialog), not a
  lane, so honesty no longer taxes healthy operation.
- The day boundary garbage-collects readiness; no stale-offer cleanup
  machinery exists to go wrong.

**Negative (accepted):**
- A restarted day bot never inherits its position (MVP): it re-enters
  on its own signals and may miss a re-entry. Accepted to keep the
  clean≠flat hole closed; adoption is the swing slice.
- A read may briefly show a computed phase that disagrees with the
  persisted one (drift flag) until the next persist point. Accepted
  for side-effect-free GETs.
- The second honest exposure resolution lives one click deep (inside
  the `resolve_exposure` dialog), not on the row. Accepted; one row,
  one button.
- Migration cost: pause/resume/latch surfaces are deleted and every
  lifecycle surface rewires to the one projection.

**Non-consequences:**
- `CleanExitReceipt` content, the strengthened clear-freeze guard
  (strictly-newer receipt, `exposure_resolution` enum), the honest
  exit taxonomy, and the false-crash backfill are unchanged from
  PRD #974 rev 2/3 — the PRD remains their authority.
- ADR-0010's poison evidence, ADR-0011's halt semantics, and
  ADR-0019's diagnostics keep their roles; they feed the evaluator as
  evidence.
- Swing/overnight support stays deferred; `exposure_resolution`'s
  `intended` member is the future-proofing for it.

## References

- PRD #974 rev 3.1 — requirements, user stories, testing seams,
  slicing (this ADR is its companion).
- `docs/audits/bot-control-panel-redeploy-observations.md` — the
  state-explosion and dead-end field evidence.
- `docs/architecture/adrs/0022-temporal-authority-calendar-and-timestamp.md`
  — calendar authority for `effective_stop` and session anchors.
- `docs/architecture/adrs/0025-single-dominant-headline-notice-placement.md`
  — the dominance pattern reused by the attention badge.
- `docs/architecture/adrs/0019-daemon-diagnostics-composed-control-plane-authority.md`
  — composed as evidence by `daemon_unreachable`.
- Branch `codex/add-account-freeze-clear` — WIP account-triage service
  (slice-2 substrate; its clean≠flat hole is closed by the PRD's
  clear-freeze guard).
