# ADR-0015: Operator Notice Contract

**Status**: Accepted
**Date**: 2026-06-23
**Supersedes**: extends ADR-0014
**PRD**: `docs/architecture/operator-notice-prd.md`

## Context

ADR-0014 established that broker-activity narratives are backend-authored.
Three live Bot Control failure modes (#656, #657, #658) still ship raw enum
strings to traders or — worse — ship nothing while the bot silently does
the wrong thing. The repair is one contract for every operator-facing
failure surface.

## Decision

All operator-facing failure surfaces emit typed `OperatorNotice` objects
composed in the Python service. The bot control page renders `title`, `message`,
and `action` verbatim and is structurally incapable of composing safety
copy from operational enums.

### Schema (canonical)

`PythonDataService/app/operator/notices/schema.py` is the single source
of truth for:

- `OperatorNoticeTier = Literal["info", "warning", "critical"]`
- `OperatorNoticeCode = Literal[...]` — namespaced (`runtime.*`,
  `watchdog.*`, `activity.*`, `reconciliation.*`); all PR 1–6 slots
  declared upfront.
- `OperatorNoticeAction.kind = Literal["none", "wait", "open_runbook",
  "focus_bot control_action", "renew_control_plane_lease",
  "external_manual_check", "redeploy"]`
- `OperatorNotice` — `code`, `tier`, `title`, `message`, `source_codes`,
  `forensic_facts`, `action`, `runbook_slug`, `occurred_at_ms`.
- `OperatorIncident` — `incident_id`, `category`, `notice`,
  `started_at_ms`, `resolved_at_ms`, `evidence`.

### Invariants

- `title` and `message` are finished English; the frontend never
  interpolates copy.
- `source_codes` references operational enums for forensics; never
  displayed as primary copy.
- `code` is namespaced; PR 1 declares every planned slot so frontend
  type generation is stable across PRs 1–6.
- `runbook_slug`, when set, must reference a file that ships in the
  same PR. No aspirational links.

### Tier policy

| Tier | Trader interpretation | Trader action |
|---|---|---|
| `info` | Expected non-trading state (market closed). | None. |
| `warning` | Degraded; bot is protecting itself. | Monitor. |
| `critical` | Safety or control failure. | Verify/reconcile before trusting the bot. |

### Action semantics

`OperatorNoticeAction.kind` separates finished notice copy from the
closed affordance the bot control page may expose:

- Navigation/focus affordances: `focus_bot control_action`, `open_runbook`,
  `redeploy`. `redeploy` routes to the existing deploy/configuration
  flow; it never silently redeploys.
- Bounded remediation affordances: `renew_control_plane_lease`. These
  actions must be explicitly named in this contract, routed through the
  data plane, and implemented as one-shot backend-authored operations.
- Non-clickable explicit non-automation: `external_manual_check`.
  "Check positions in IBKR" must not look like the bot control page performed
  reconciliation.
- `none` / `wait` carry no affordance.

### Persistence model

- **Ephemeral projection notices** (runtime freshness, activity health):
  recomputed each operator-surface poll; not persisted.
- **Incident notices** (watchdog halt, publisher lifecycle): persisted
  as `OperatorIncident` JSON at
  `artifacts/live_runs/<run_id>/operator_incidents/<incident_id>.json`.
  Schema lands in PR 1; first writer lands in PR 2.

### Exhaustiveness gate

Every closed enum reaching the bot control page through a notice is
parametrized-tested against the rules table. A snapshot test pins the
`OperatorNoticeCode` union; frontend types cannot drift silently.

## Consequences

- Backend owns trader-facing copy. Adding a new failure mode requires a
  backend rule + a snapshot update.
- The frontend renderer is dumb about safety semantics: it renders
  backend-authored copy and may dispatch only closed backend-authored
  action kinds. Code-driven safety decisions or ad-hoc copy in the UI
  remain out of bounds.
- ADRs 0013 and 0014 carry forward; this generalizes their principle.

## Implementation

Initial implementation: PR 1 (this commit's diff) — runtime freshness.
PRs 2–6 reuse the contract; see PRD §5.

## Amendment 2026-07-08 — honest actionability and mandatory resolution

**Context:** The 2026-07-08 grilling session ("if the operator can take no
corrective measure from the cockpit, what use is the message?") found that
the contract permits untruthful notices in three ways:

1. The tier table promised trader actions the schema never enforced —
   `critical` with `action.kind="none"` is legal while the table claims
   "Verify/reconcile before trusting the bot."
2. `action.kind="none"` conflates "no action *needed*" with "no action
   *exists*" — states that demand opposite operator responses and are
   indistinguishable on screen.
3. No notice is required to state how it resolves. `wait` names no
   condition; `external_manual_check` ships with an optional `target`
   ("Check positions in IBKR" — then what? when does it clear?).

**Decision:**

### 1. Tier is trust-impact only

The "Trader action" column of the tier policy is deleted. Tier answers
"how much should the operator distrust the bot right now" — never "what
should the operator do." Replacement table:

| Tier | Trader interpretation |
|---|---|
| `info` | Expected state. No trust impact. |
| `warning` | Bot is protecting itself. Its claims remain trustworthy; its activity is reduced. |
| `critical` | Trust impact: a claim this console normally makes (fills, positions, command responsiveness) cannot be trusted until the notice resolves. |

`critical` with no remedy is a first-class, legal state. The contract no
longer implies every critical failure has a verification path; when none
exists, the notice says so plainly.

### 2. Required `actionability` classification

Every notice carries
`actionability: Literal["actuatable", "routed", "self_resolving", "no_remedy"]`:

- **`actuatable`** — the cockpit performs or directly navigates to the
  fix. Legal `action.kind`: `renew_control_plane_lease`,
  `focus_cockpit_action`, `redeploy`.
- **`routed`** — a fix exists but lives elsewhere. Legal `action.kind`:
  `open_runbook`, `external_manual_check`. The destination and what to
  look at there are mandatory (non-null `target`).
- **`self_resolving`** — no operator action is needed; the system clears
  it. The clearing condition is mandatory in `resolution`. Subsumes and
  retires `action.kind="wait"`.
- **`no_remedy`** — no action exists anywhere, cockpit or otherwise. The
  message must state what the operator must not trust meanwhile.
  Carries a required sub-classification
  `remedy_status: Literal["inherent", "unbuilt"]`:
  - `inherent` — no remedy can exist (e.g. the WAL is unobservable);
    requires a one-line justification of why, in the notice's authoring
    table.
  - `unbuilt` — a remedy is conceivable but not built; requires a
    corresponding entry in `docs/known-gaps.md`. The label is truthful
    today, but the gap stays visible instead of becoming comfortable.

`action.kind="none"` survives only as "no clickable affordance"; its
meaning is disambiguated by `actionability`. The `wait` kind is retired.

### 3. Mandatory `resolution` statement

Every notice carries a backend-authored `resolution`: the condition under
which the notice clears and who observes it. Examples:
"Clears automatically when a fresh IBKR bar arrives."
"Clears after you confirm positions at IBKR and run Reconcile."
"Resolution unknown — requires manual reconciliation."

"Resolution unknown" is a legal, truthful value; omission is not. A
notice whose author cannot state its resolution condition is not ready
to ship.

### 4. Consistency constraints (enforced by the exhaustiveness gate)

`actionability` × `action.kind` pairings outside the table in §2 fail
validation. `routed` without a named destination fails validation.
`self_resolving` whose `resolution` does not name a clearing condition
fails review. `no_remedy` without `remedy_status` fails validation;
`remedy_status="unbuilt"` without a cross-referenced
`docs/known-gaps.md` entry fails the exhaustiveness gate. The snapshot
test pins `actionability` (and `remedy_status` where applicable) per
notice code alongside the existing code-union pin.

### 5. Correction

The action-kind literal rendered in this ADR's original text as
`focus_bot control_action` is a transcription error; the canonical
literal is `focus_cockpit_action`
(`app/operator/notices/schema.py`).

### 6. Reserved codes for known silent states

The contract's failure mode is not only untruthful messages — it is
also **missing** ones: states with real trust impact that emit nothing.
Following this ADR's original declare-slots-upfront pattern, the
migration PR adds reserved codes for the known silent-critical states,
pre-classified so the exhaustiveness gate, placement function
(ADR-0025), and frontend type generation see them before any
implementation exists:

- `fleet.sibling_liveness_unproven` — crashed sibling shows ACTIVE
  indefinitely (`critical`, `routed` to retire/replace recovery,
  cross-referenced in `docs/known-gaps.md`).
- `reconciliation.divergence_while_submitting` — account-truth verdict
  diverges while the bot continues submitting (`critical`, `routed` to
  Account Monitor reconciliation, cross-referenced in
  `docs/known-gaps.md`). The rung receipt (Amendment 2026-07-08 (b))
  covers this at mutation time only; this code covers the running bot
  between clicks.

Newly discovered silent states follow the same route: reserve the code
with an honest pre-classification first, implement second. A
known-gaps entry describing an operator-invisible trust impact without
a corresponding reserved notice code is itself a gap.

**2026-07-10 closeout.** The two underlying safety hazards are now fail-closed,
so they no longer appear in `docs/known-gaps.md`: cached Account Truth gates
every durable submit, and daemon boot retires prior `ACTIVE` bindings whose
process liveness it cannot own. The codes remain reserved for a future
notification layer; reservation status is not authority to bypass either
enforcement path.

**Scope:** governs every new notice immediately; existing notice
families (`runtime.*`, `watchdog.*`, `activity.*`, `reconciliation.*`,
`broker_session.*`, `order.*`, `submit.*`, `safety_halt.*`) migrate in a
dedicated implementation PR. Placement and prominence rules — which
surface renders which tier × actionability combination — are
deliberately out of this amendment's scope (open item from the
2026-07-04 operator-observability taxonomy memo; resolved the same day
by ADR-0025).

## Amendment 2026-07-08 (b) — rung receipts on mutation responses

**Context:** Field observation (2026-07-08 grilling, defect class "c"):
an operator clicked Resume, the durable write succeeded, and the bot
still never ran — a downstream gate (crash recovery, host offline) was
always going to block the actual restart, and no surface connected the
click to that next blocker. Every individual surface told the truth;
the page still lied by omission. The blockage ladder already computes
an ordered rung list with a current rung on the status projection; it
was never consulted at mutation time.

**Decision:** every mutation response (Resume, Start, Reconcile,
Flatten-and-pause, crash-recovery override, Mark Poisoned) carries a
backend-authored **rung receipt**: a notice-shaped statement naming the
next blocking rung from the blockage ladder — or the scoped all-clear.
The receipt inherits this ADR's full contract: tier, actionability,
mandatory resolution statement, verbatim rendering.

Example (the observed defect, repaired):
"Stop latch cleared. The bot still won't run: previous host runner
crashed — record crash-recovery evidence." — `actionability:
actuatable`, the crash-recovery override inline.

**Required constraints:**

1. **The receipt claims only what the ladder enforces.** The all-clear
   wording is scoped — "no enforced gate blocks the next start" — never
   an unscoped "all clear." When an observational (non-gating) verdict
   disagrees (e.g. account-truth, which `docs/known-gaps.md` records as
   gating nothing), the receipt surfaces it as a `warning` alongside
   the scoped all-clear. A receipt asserting more than the enforcement
   layer guarantees is a contract violation.
2. **Computed at mutation time.** The rung receipt is authored from a
   fresh ladder evaluation inside the mutation request, never from the
   client's pre-click poll snapshot — otherwise it inherits exactly the
   staleness that produced the defect.
3. **One resolver.** The mutation-time ladder evaluation and the status
   projection's ladder are the same code path (ADR-0013 §6 shared
   resolver pattern); a parallel implementation is a violation.
4. **Response shape.** Receipts extend the existing structured mutation
   response models (e.g. `SetInstanceDesiredStateResponse.durable` /
   `.actuation` gain a receipt sibling); they do not replace the
   existing acknowledgement fields.
