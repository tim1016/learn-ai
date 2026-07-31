# PRD — Alpaca Bot Fleet, Control, and Deploy Redesign

- **Date:** 2026-07-31
- **Status:** Ready for issue approval
- **Product surfaces:** Alpaca Bots roster, Trader bot details, Operator bot details, broker Deploy
- **Delivery posture:** Three polished vertical slices; no standalone styling, contract, or performance cleanup issues
- **Builds on:** the broker-v2 panel contracts, the Alpaca Account Clerk, ADR 0023, ADR 0030, ADR 0032, the broker-v2 control-panel design, and the Clerk-governed bot-control PRD
- **Authority:** Python authors numerical values, lifecycle meaning, admission verdicts, blockers, confirmations, and receipts. Angular renders those contracts and performs no trading arithmetic.

---

## 1. Executive summary

The Alpaca bot-control data plane is no longer a prototype. Alpaca paper bots can
be deployed through Clerk custody, started, stopped, flattened, reconciled, and
recovered from holds. Recent paper validation proved eight concurrent bots, thirty
filled orders, eleven fault scenarios, and clean terminal reconciliation.

The user interface does not yet communicate that authority reliably. The Bots
roster mixes incompatible theme tokens, renders a structurally misaligned table,
and withholds useful state explanations. The details page has Trader and Operator
lenses, but it lacks a persistent identity and decision header, omits important
trading context, and visually conflates current enforcement gates with historical
transaction evidence. Some presented actions have no executable performer, while
destructive confirmation copy is displayed without actually requiring a
confirmation. Alpaca deployment is a modal with a hard-coded strategy even though
the architecture requires one broker Deploy page backed by accepted validation
evidence.

This product redesign makes the Alpaca experience an operator-grade fleet console:

1. The roster answers which bots need attention and what can safely happen next.
2. The Trader lens answers what the bot is doing and how it is performing.
3. The Operator lens explains the exact authority, blocker, evidence, and recovery
   path behind bot and account state.
4. Deploy creates a bot from a real, accepted strategy-validation record through
   one broker-aware page.
5. Every visible action and gate is backed by the production command path that it
   claims to represent.

The work ships as three substantial vertical slices to keep the review count low.
Performance improvements are included where they are necessary to make each slice
usable rather than separated into a broad optimization project.

## 2. Grounding and observed problems

### 2.1 Theme and contrast are internally inconsistent

The application already defines a dark trading-console palette in
`Frontend/src/app/styles/_tokens.scss`. The broker-v2 components instead mix:

- Prime surface variables such as `--p-surface-*` with light fallbacks;
- undefined `--surface-card` and `--text-color*` variables;
- undefined `--color-surface` and `--color-text*` variables.

During live inspection at 1440 by 900, the Alpaca account strip rendered a
near-white inherited value color over a near-white fallback background. Roster
header text was muted gray over a near-white header. Operator cards fell back to
white while the surrounding shell remained dark. These are deterministic token
failures, not subjective palette preferences.

The redesign uses only the existing application tokens:

| Role | Token |
| --- | --- |
| Canvas | `--bg-canvas` |
| Primary surface | `--bg-surface` |
| Elevated surface | `--bg-elevated` |
| Hover and selected surface | `--bg-hover` |
| Borders | `--border`, `--border-subtle` |
| Primary and secondary text | `--text-primary`, `--text-secondary` |
| Product accent | `--accent` |
| Positive, negative, warning | `--bull`, `--bear`, `--warn` |

Color is never the only carrier of bot, gate, health, or P&L state.

### 2.2 The roster is not a stable table

The current roster renders the header and virtualized body as separate tables.
Only the body uses fixed table layout, and the actions cell becomes a flex
container. In the live page the body columns were equal-width while header columns
used content width, so headers and values did not align. Nine columns compress bot
identity, timestamps, and action labels beyond useful scanning.

The first account-scoped load is also slow. The account lookup is measured at
five to fifteen seconds in the panel data source, while route resolution, the
account strip, and account-scoped catalog requests can repeat or wait on the same
account posture. Catalog projection also reads the order journal and the latest
decision journal for each bot on recurring refresh.

The paper validation audit measured roster action latency across fifteen actions:

| Statistic | Milliseconds |
| --- | ---: |
| Minimum | 9,404 |
| Median | 14,531 |
| Mean | 14,493 |
| Maximum | 18,561 |

The redesign must paint a useful shell immediately, retain the last good snapshot
during refresh, expose freshness, and remove redundant account resolution from the
first-paint path. Deeper transport changes are required only when profiling shows
that these changes do not meet the budgets in this PRD.

### 2.3 Trader and Operator lenses lack a common decision header

The details shell has no persistent breadcrumb, bot identity, strategy, account,
execution mode, or freshness header. Its Trader lens prioritizes two large chart
panes even when the bot is stopped and has no chart or trade data. Realized and
open P&L can disappear when there are no fills, even though zero or current values
are meaningful. Exposure, working orders, decision freshness, execution-session
policy, and account custody are not presented together.

The Operator lens can overflow horizontally because its grid does not allow the
main column to shrink below the transaction rail's minimum content width. It
combines nested scroll regions, causing the journal to fall below the useful
viewport. Each panel poll can also trigger another evidence-tail load.

The lens switch is presentation-only and is not a security boundary. Operator
authorization must be enforced by the server for privileged actions.

### 2.4 Presented controls exceed executable controls

The production performer map currently executes:

- `start`;
- `stop`;
- `flatten_stop`;
- `reconcile_now`;
- `clear_hold`.

The policy can also present `retire` and `cancel_order`, but neither has a
production performer in that map. The cancel-order policy is not scoped to a
specific working order. Disabled guards return empty blocker collections, so the
UI cannot explain why Start, Flatten and stop, or Clear hold is unavailable.

Backend confirmation contracts are currently `null`. Even when a confirmation is
provided, the frontend prints its body beneath the button and executes immediately
on click. This is not confirmation.

The redesigned surface never displays an enabled action that cannot execute. It
does not add a Pause label: there is no separate durable PAUSED behavior in the
current Alpaca lifecycle. Stop remains the honest control until a future lifecycle
decision defines distinct pause semantics end to end.

### 2.5 Transaction evidence is not the current gate set

The six stations — Signal, Intent, Submit gate, Broker acknowledgement, Fill, and
Reconciled — describe the evidence progression of one transaction. Signal, Intent,
Acknowledgement, Fill, and Reconciled are not admission gates. The station
projection currently authors every `blocker` as `None`; selecting an older
transaction changes only the displayed transaction reference and does not request
that transaction's station evidence from the server.

The Account Clerk is the actual admission authority. Current readiness and past
transaction evidence therefore become two explicitly separate products:

- **Readiness gates:** current enforcement-backed predicates that block a named
  operation such as start, resume, submit, deploy, or recovery.
- **Transaction trace:** historical evidence for one selected intent/order
  transaction.

Every displayed readiness gate must be evaluated by the same production code path
that admits or rejects the named operation.

### 2.6 Alpaca deploy duplicates the single Deploy architecture

The Alpaca dialog currently exposes one closed strategy,
`deployment_validation`, and authors a long-stock entry/exit plan in the backend.
It correctly gates paper deployment on account posture, Clerk freezes and holds,
outstanding intents, and channel health.

ADR 0023 requires exactly one Deploy page and requires its strategy selector to
contain only current validation events that are both human-validated and
`accepted_for_deploy`. The older Interactive Brokers Deploy form contains the
right product concepts — deployment identity, strategy evidence, account,
execution mode, signal stream, action plan, sizing, readiness, review, and launch
receipt — but is too large and broker-specific to copy.

The redesign creates one broker-aware Deploy page composed from bounded sections
and broker-authored capabilities. `Deployment Validation` becomes a real
catalog-backed deployable strategy with validation provenance rather than a
special-case option that exists only inside the Alpaca deploy service.

## 3. Product principles

1. **Truth before convenience.** A visible action or gate is backed by the exact
   production authority it names.
2. **One fleet scan.** The roster supports fast attention triage without forcing a
   details visit for routine status.
3. **One current verdict.** Each action surface leads with a backend-authored
   allowed or blocked outcome, explanation, and next step.
4. **History is labeled as history.** Transaction evidence never masquerades as a
   current admission decision.
5. **Trader and Operator share identity, not density.** Both lenses show the same
   bot/account identity and current mission state; Operator adds custody and
   evidence detail.
6. **Destructive actions show blast radius.** Flatten and stop, Retire, and similar
   irreversible controls require an explicit backend-authored confirmation.
7. **One Deploy product.** Broker variations are capabilities within one page, not
   independent deploy implementations.
8. **Perceived performance is product behavior.** The shell, last-known snapshot,
   freshness, and action progress remain understandable while external calls are
   slow.

## 4. Users and jobs

### Trader

- Find bots that need attention.
- Understand whether a bot is working, stopped, blocked, or exposed.
- See current exposure, P&L, orders, fills, and recent decisions.
- Start or stop a bot with an accurate statement of the consequence.
- Deploy an accepted strategy into the selected paper account.

### Operator

- Identify which authority blocks a specific operation now.
- Recover holds, reconcile account truth, and reduce attributed exposure.
- Inspect the complete evidence trail for one selected transaction.
- Distinguish bot lifecycle, runtime liveness, Clerk custody, and broker truth.
- Receive a durable receipt for every attempted control action.

## 5. Information architecture

### 5.1 Alpaca Bots roster

The page header contains:

- `Alpaca Bots`;
- selected account ID and PAPER mode;
- explicit refresh and last-updated state;
- one `Deploy strategy` primary link.

The account posture strip shows the minimum fleet-wide context:

- broker account status;
- reconciliation outcome and timestamp;
- Clerk hold/freeze outcome;
- market-data and execution channel health.

Account equity, cash, and buying power remain available but do not outrank custody
and readiness.

The desktop roster uses one aligned semantic table or grid with six columns:

| Column | Content |
| --- | --- |
| Bot | Symbol, strategy instance ID, strategy label, execution mode |
| State | Status label, attention state, one current reason |
| Exposure | Side and attributed quantity, or Flat |
| Today | Realized P&L, open P&L, fill count |
| Last activity | Local timestamp plus freshness semantics |
| Action | One labeled routine action plus overflow actions |

Filters are All, Working, Off duty, Needs attention, and Retired. Search matches
symbol, instance ID, and strategy label. No bots, no filter matches, loading,
stale-data, and unavailable states are distinct.

At compact widths each row becomes a structured list item without hiding its
state reason or primary action. Virtualization is used only when necessary and
must preserve header alignment and accessible navigation.

### 5.2 Shared bot-details header

Both lenses share:

- breadcrumb back to the account roster;
- symbol, strategy instance ID, strategy label, and execution mode;
- broker account identity;
- lifecycle and runtime state;
- last updated time;
- a mission strip such as `Off duty · Flat · Safe to start` or
  `Working · New entries blocked by market-data health`;
- lens state reflected in `?lens=trader|operator`.

The tabs implement complete tab semantics and keyboard behavior. The selected
lens does not grant authorization.

### 5.3 Trader lens

Trader renders, even when values are zero:

- attributed position and exposure;
- realized P&L today;
- open P&L;
- working-order and fill counts;
- primary lifecycle action and its current blocker/consequence;
- latest strategy decision and evaluated bar freshness;
- execution-session/RTH policy;
- recent decisions and fills;
- compact account/Clerk custody posture.

Active bots may retain live and history chart views. When chart data is absent or
the bot is off duty, charts no longer dominate the page and the empty state
explains what event will populate them.

### 5.4 Operator lens

Operator orders information by recovery value:

1. Current verdict and recovery action.
2. Enforcement-backed readiness gates for the selected operation.
3. Lifecycle/runtime and Clerk/account custody summaries.
4. Working orders and attributed exposure with available reducing actions.
5. Transaction trace for the selected transaction.
6. Journal/evidence records and the evidence drawer.

Selecting a transaction requests the server projection for that exact reference.
The page has one predictable scroll owner at desktop sizes and does not overflow
at a 1440 by 900 viewport.

## 6. Action and readiness contracts

### 6.1 Presented action

For every closed action ID, the server authors:

- label and explanation;
- whether the action is currently executable;
- blockers when it is not executable;
- confirmation when its consequence is destructive;
- action scope and affected account/bot/order identifiers;
- stable revision/idempotency material;
- the next safe action.

The presented-action registry and executable performer registry must not drift.
An action without a performer is never presented as enabled.

### 6.2 Action semantics

| Action | Required meaning |
| --- | --- |
| Start | Start a new run from durable deployment configuration after a fresh readiness decision |
| Stop | Persist STOPPED, stop strategy evaluation, cancel bot-owned working entries, leave attributed exposure untouched |
| Flatten and stop | Persist STOPPED first, then ask the Clerk to prove or reduce attributed exposure to flat |
| Reconcile now | Run one Clerk reconciliation sweep and return its durable result |
| Clear hold | Clear the current recoverable hold only when the backend presents it as safe |
| Cancel order | Cancel one identified working order through Clerk custody |
| Retire | Terminal lifecycle operation implemented end to end before it is exposed |

Flatten and stop and Retire require an explicit confirmation interaction. The
confirmation states the account, bot, affected exposure/orders, irreversible
effect, and any required reason. The UI presents success, failure, conflict, and
unknown-outcome receipts without converting an uncertain result into failure or
success.

### 6.3 Readiness gate

A displayed gate includes:

- stable `gate_id` rendered through the shared receipt-label vocabulary;
- human label and backend-authored explanation;
- outcome: allowed, blocked, stale, or unknown;
- operation blocked, such as start, resume, submit, deploy, or recovery;
- scope: account, bot, run, or order;
- enforcing authority and evidence reference;
- `evaluated_at_ms` as int64 milliseconds UTC;
- next safe action or cure where one exists.

The UI does not derive a composite readiness answer. It renders one backend
verdict and its ordered checks.

### 6.4 Transaction trace

The transaction trace remains Signal, Intent, Submit admission, Broker
acknowledgement, Fill, and Reconciled where evidence exists. It is explicitly
labeled historical. Each station links to durable evidence and may be waiting,
satisfied, blocked, stale, or unknown without being called a current gate.

## 7. Broker-aware Deploy

There is one Deploy route in the Broker area. The page receives broker/account
context and renders sections for:

1. Deployment identity and accepted strategy.
2. Execution mode and account.
3. Signal stream and action plan.
4. Sizing and carryover/risk policy.
5. Current deployment readiness.
6. Review and launch receipt.

For Alpaca phase one:

- account mode is paper and is broker-authored;
- live execution is unavailable;
- `Deployment Validation` is a real strategy-catalog entry whose current
  validation event is `validated` and `accepted_for_deploy`;
- the server authors the one long-stock ENTER leg and matching EXIT summary;
- safe-canary one-share and bounded whole-share sizing remain available;
- carryover defaults off and follows account policy;
- account, Clerk, intent, and channel checks are visible as readiness gates;
- deployment creates the durable binding and navigates to its bot details receipt.

The Alpaca Bots CTA and Alpaca sidebar entry both navigate to this page. The
broker-specific deploy dialog is removed. Broker differences are expressed by
server capabilities, not duplicated page implementations.

## 8. Performance and refresh requirements

- A route shell and roster skeleton appear without waiting for the broker account
  call.
- The same resolved account posture is shared by route scope, the account strip,
  and the first catalog projection.
- Refresh keeps the last successful roster/panel visible and identifies it as
  refreshing or stale.
- The visible lens controls optional data work: Operator does not fetch chart
  history, and Trader does not fetch evidence tails.
- Operator evidence reloads only when the selected transaction or journal-tail
  sequence changes.
- A list action does not make an avoidable full-panel read immediately before the
  mutation; revision and blocker semantics remain server-enforced.
- Instrumentation records first useful roster paint, fresh roster paint, panel
  paint, and action round-trip time.

Targets under a healthy local paper environment after the first broker connection:

| Measure | Target |
| --- | ---: |
| Route shell | 500 ms |
| Cached/last-good roster paint | 1 s |
| Fresh roster paint | 3 s |
| Routine action acknowledgement | 3 s |

If Alpaca itself exceeds the fresh-data target, the interface must continue to
show the last known snapshot, freshness, and current request state honestly.

## 9. Accessibility and responsive behavior

- Text and interactive controls meet WCAG AA contrast.
- Status never relies on color alone.
- Every icon-only control has a visible label or accessible name; routine bot
  actions use visible text.
- Row navigation is a real link or an equivalent single semantic target rather
  than a focusable table row containing nested competing controls.
- Tabs, menus, dialogs, confirmations, and evidence drawers are keyboard operable
  with managed focus.
- Loading and action outcomes use appropriate live regions without announcing
  every poll.
- The primary information and routine action remain usable at 320 CSS pixels;
  desktop Operator content fits without horizontal page overflow at 1440 by 900.

## 10. Out of scope

- Alpaca live-money trading.
- New strategy mathematics or frontend-computed trading values.
- A new Account Clerk or separate gate service.
- A cosmetic Pause alias without new durable lifecycle semantics.
- Rebuilding the Interactive Brokers control panel in this delivery.
- Replacing polling with a new streaming infrastructure unless measurement proves
  it is required for the stated budgets.
- Copying the existing monolithic Interactive Brokers Deploy component.

## 11. User stories

1. As a trader, I can scan the Alpaca fleet and immediately identify bots that
   need attention and their next safe action.
2. As a trader, I can open a bot and see its identity, account, current mission,
   exposure, P&L, orders, fills, decisions, and execution policy without switching
   to Operator.
3. As an operator, I can see the exact current readiness decision that blocks an
   operation and the production authority and evidence behind it.
4. As an operator, I can select an older transaction and inspect its actual server
   projected trace and journal evidence.
5. As an operator, every enabled action executes, every disabled action explains
   why, and every destructive action requires confirmation.
6. As a trader, I can deploy Deployment Validation from accepted validation
   evidence using the same broker Deploy product as other strategies.
7. As a trader, slow broker calls do not blank the fleet or leave me wondering
   whether an action was accepted.

## 12. Delivery slices

### Slice 1 — Fleet roster and shared account posture

Deliver the complete Alpaca fleet scan: application-token visual foundation,
account posture, six-column responsive roster, attention filtering, truthful
loading/error/freshness states, labeled routine controls, and shared account
bootstrap. Measure the first-paint path and eliminate redundant account resolution
that prevents the target experience.

### Slice 2 — Truthful Trader and Operator control panel

Deliver the shared identity/mission header, complete Trader view, recovery-first
Operator view, executable-action parity, blockers, real destructive confirmation,
current readiness gates, historical transaction traces, real transaction
selection, durable action receipts, and stable responsive/scroll behavior.

### Slice 3 — One broker-aware Deploy page

Deliver one Broker Deploy page, migrate Alpaca from its dialog, source Deployment
Validation from accepted strategy evidence, render broker-authored Alpaca
capabilities/readiness, launch the durable deployment, and link its receipt to the
new bot details page. Preserve the Interactive Brokers behavior while decomposing
shared sections rather than duplicating its monolith.

## 13. Definition of done

- The three slices are demoable independently and together form one coherent
  Alpaca fleet-control workflow.
- No enabled action lacks a production performer.
- Every disabled routine action has a backend-authored blocker and next step.
- Destructive controls cannot execute from a single unconfirmed click.
- Current readiness and historical transaction evidence are separate in contract,
  language, and layout.
- Trader and Operator meet the information requirements in this PRD and preserve
  server-authored numerical and safety meaning.
- Alpaca uses one broker Deploy page and no broker-specific deploy modal.
- Deployment Validation is backed by current accepted validation provenance.
- Roster and details remain understandable during slow broker calls and meet the
  refresh/performance behavior in this PRD.
- Frontend work passes lint, focused Vitest suites, accessibility assertions, and
  visual verification at desktop and compact widths.
- Python contract/action work passes Ruff and focused pytest suites, with
  regression coverage for every corrected action, blocker, gate, and deploy path.
- All timestamps remain int64 milliseconds UTC at wire/storage boundaries, and raw
  backend identifiers in evidence UI use the shared `receiptLabel` pipe.

## 14. Grounding references

- `Frontend/src/app/styles/_tokens.scss`
- `Frontend/src/app/components/broker/v2-panel/bots-list-page/`
- `Frontend/src/app/components/broker/v2-panel/bots-roster/`
- `Frontend/src/app/components/broker/v2-panel/account-strip/`
- `Frontend/src/app/components/broker/v2-panel/panel-shell/`
- `Frontend/src/app/components/broker/v2-panel/trader-lens/`
- `Frontend/src/app/components/broker/v2-panel/operator-lens/`
- `Frontend/src/app/components/broker/v2-panel/deploy-dialog/`
- `Frontend/src/app/components/broker/broker-deploy-form/`
- `PythonDataService/app/broker/v2panel/action_policy.py`
- `PythonDataService/app/services/broker_v2_panel/panel_data_source.py`
- `PythonDataService/app/services/broker_v2_panel/paper_deploy_service.py`
- `PythonDataService/app/services/broker_v2_panel/station_derivation.py`
- `docs/audits/alpaca-clerk-paper-validation-2026-07-31.md`
- `docs/architecture/bots-first-paint-performance.md`
- `docs/architecture/adrs/0023-strategy-validation-human-flag-over-engine-match-deploy-rehomed-to-bots.md`
- `docs/architecture/strategy-validation-deploy-rehome-prd.md`
- `docs/prds/alpaca-clerk-governed-bot-control.md`
- `docs/superpowers/specs/2026-07-29-broker-v2-bot-control-panel-design.md`
- `docs/superpowers/specs/2026-07-30-alpaca-clerk-governed-bot-execution-design.md`
