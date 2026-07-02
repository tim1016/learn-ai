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
