# Non-numeric operator-verdict census

**Charter:** [#1645](https://github.com/tim1016/learn-ai/issues/1645)

**Question.** After subtracting ADR 0036, ADR 0041, the shared
`receiptLabel` rule, and ADR 0027, does the live Alpaca product still need a
successor to ADR 0013's rule that Angular does not author operator verdicts?

**Answer.** No new ADR is owed. The four named subtractions do not exhaust the
space, but the missing authority is not actually missing:
[ADR 0035 Decision 12](../architecture/adrs/0035-alpaca-clerk-sqlite-event-sourced-authority.md)
already says that, for the live Alpaca custody product, the backend authors
safety and capability — explicitly including which action is primary, why an
action is disabled, and freshness — while Angular renders.
[ADR 0027](../architecture/adrs/0027-operator-blocker-disposition-taxonomy.md)
already owns blocker disposition, copy, and the primary move. Those accepted, live
decisions cover the three violations this census confirmed. Recreating ADR 0013
under a new number would duplicate their rule while reintroducing the retired
IBKR `operator_surface` vocabulary.

The implementation is not fully conformant. Three live Angular seams still make
operator judgments:

1. the Alpaca Account Desk derives a dominant posture and disposition from raw
   Clerk projection fields; and
2. the live Account Strip combines account and custody facts into availability
   verdicts, including a false-positive “Trading available” case; and
3. both per-bot lenses choose their primary lifecycle action from health fields.

The two account-level seams share one canonical backend account-posture fix;
the per-bot action seam is independent. Both fixes have filed issues below. No
new domain vocabulary or `CONTEXT.md` change is owed.

**Date verified:** 2026-08-18. **Code read at:** `a16571c2736b`.

## Scope and classification test

The product scope is the live Alpaca Broker V2 operator system named by map
[#1588](https://github.com/tim1016/learn-ai/issues/1588):
`/brokers/alpaca`, its account-scoped bot roster, per-bot Trader and Operator
lenses, Gallery, and the deploy drawer. Analytics such as strategy-validation
and indicator-reliability verdicts are not operational bot-control judgments
and are outside this charter.

A candidate is a **verdict** when it combines facts into an operational answer
or chooses what the operator should do: healthy/blocked/review/terminal,
attention routing, action priority, or remediation disposition. It is
**presentation** when Angular formats an already-authored classification,
renders independent facts without combining them, or describes browser-local
transport/form state. This is the same boundary ADR 0013 used, applied only as
a classification tool; ADR 0013 itself remains superseded.

The census searched production `Frontend/src/app` TypeScript and templates for
`computed()` expressions over backend fields, conditionals over verdict/state/
status enums, safety/readiness/health/attention language, and prose selected by
ternaries or switches. Every candidate was then traced through the registered
route and, where relevant, the activated-SQLite adapters. Tests and fixture-only
routes were not counted as live product paths.

### Primary-source evidence anchors

| Claim | Owning source |
|---|---|
| Frontend safety/capability/primary-action boundary | [ADR 0035 Decision 12](../architecture/adrs/0035-alpaca-clerk-sqlite-event-sourced-authority.md#L187-L192) |
| Blocker disposition, copy, and move ownership | [ADR 0027 contract](../architecture/adrs/0027-operator-blocker-disposition-taxonomy.md#L25-L69) |
| V1 classification and action choice | [`projectionPosture`](../../Frontend/src/app/components/brokers/alpaca-desk/alpaca-operator-posture.component.ts#L90-L140) and [its authored disposition copy](../../Frontend/src/app/components/brokers/alpaca-desk/alpaca-operator-posture.component.html#L35-L56) |
| V1 activated-SQLite reachability | [registered routes](../../Frontend/src/app/app.routes.ts#L269-L285), [authority selection and snapshot request](../../Frontend/src/app/components/brokers/alpaca-desk/alpaca-operator-lens-data.service.ts#L24-L40), and [live projection binding](../../Frontend/src/app/components/brokers/alpaca-desk/alpaca-operator-lens.component.html#L14-L21) |
| V2 classification and consumers | [`primaryLifecycleAction`](../../Frontend/src/app/components/broker/v2-panel/bot-detail-banner/lifecycle-action.ts#L12-L19), [Trader banner](../../Frontend/src/app/components/broker/v2-panel/trader-lens/trader-bot-banner/trader-bot-banner.component.ts#L40-L56), [Operator banner](../../Frontend/src/app/components/broker/v2-panel/operator-lens/operator-bot-banner/operator-bot-banner.component.ts#L48-L66), and [the repeated choice](../../Frontend/src/app/components/broker/v2-panel/operator-lens/operator-lens.component.ts#L104-L110) [passed into readiness suppression](../../Frontend/src/app/components/broker/v2-panel/operator-lens/operator-lens.component.html#L9-L16) |
| V2 contract and activated-SQLite adapter | [`BotPanelView`](../../PythonDataService/app/schemas/broker_v2_panel.py#L359-L388) has no banner-action reference; [`adapt_sqlite_panel`](../../PythonDataService/app/services/broker_v2_panel/sqlite_panel_adapter.py#L54-L97) authors the adapted actions and mission verdict |
| V3 account verdict and reachability | [Account Strip verdict copy](../../Frontend/src/app/components/broker/v2-panel/account-strip/account-strip.component.html#L29-L47) is mounted by the [live bot roster](../../Frontend/src/app/components/broker/v2-panel/bots-list-page/bots-list-page.component.html#L39); backend account readiness additionally requires [paper mode and active status](../../PythonDataService/app/services/broker_v2_panel/paper_deploy_service.py#L99-L107) |
| Runtime observations used only to confirm reachability | [Live operator surface inventory](live-operator-surface-inventory-2026-08-18.md#L100-L116) |

## The four required subtractions

| Existing authority | What it removes from this census |
|---|---|
| [ADR 0036](../architecture/adrs/0036-single-flatness-boundary-backend-owned.md) | Exposure/flatness classifications. On the activated path, `adapt_sqlite_panel` and `adapt_sqlite_catalog` filter exposure with `position_quantity_is_nonzero`; Angular's `Flat` text is a rendering of the resulting empty projection, not a second numeric boundary. |
| [ADR 0041](../architecture/adrs/0041-generated-operator-button-reference.md) | Button-reference and glossary copy. The manual's generated blocks are not runtime verdict derivation. |
| [`receiptLabel` rule](../../CLAUDE.md#L72) | Closed backend identifiers mapped to labels or supporting colour. This includes receipt codes, gate ids, phase/state labels, and channel names; opaque evidence stays verbatim and backend prose is not piped. |
| [ADR 0027](../architecture/adrs/0027-operator-blocker-disposition-taxonomy.md) | `OperatorBlocker` condition identity, severity, disposition, copy, and moves. Components which render those fields verbatim are conformant. |

## Active authority after subtraction

ADR 0035 is the live rule this charter's premise omitted. Decision 12 says:

> The frontend derives no safety. The backend authors capability — including
> which action is primary, why each is disabled, and freshness/staleness.

That decision is narrower and more useful than reviving ADR 0013. It governs
the Alpaca custody/control product that exists today without restoring the
retired IBKR `operator_surface` artifact. ADR 0027 supplies the reusable
backend-authored blocker shape when the answer is a blocker.
[ADR 0014](../architecture/adrs/0014-broker-authored-operator-view-backend-rendered-narratives.md)
separately owns authored execution narratives. Together these are sufficient authority;
the three findings below are implementation gaps, not undecided architecture.

## Confirmed live violations

### V1 — Account Desk derives the dominant Clerk posture and disposition

**Severity:** medium. The mutation endpoints still recheck their own gates, so
this cannot create broker authority. It can, however, give the operator the
wrong dominant posture, cure class, or escalation language.

**Frontend derivation.** `projectionPosture` in
`Frontend/src/app/components/brokers/alpaca-desk/alpaca-operator-posture.component.ts`
selects a primary recovery action and computes `healthy`, `fix_here`, `wait`,
`review`, or `terminal` from:

- `guidance.action_required`;
- `projection.uncertainties.length`;
- `projection.authority_health`; and
- `RecoveryCapability.available` / `primary`.

The template then authors the operational sentences “Waiting for fresh account
evidence”, “Review the current evidence”, and “Manual escalation required” from
that client-derived disposition.

**Reachability.** `/brokers/alpaca` loads `AlpacaDeskComponent`. Selecting the
Operator lens calls `AlpacaOperatorLensDataService.loadOnce`; a Clerk status
with `authority_kind == "sqlite"` fetches
`/api/alpaca-clerk-sqlite/accounts/{account_id}/snapshot`, and
`alpaca-operator-lens.component.html` passes that live projection directly to
`app-alpaca-operator-posture`. The
[2026-08-18 rendered-surface inventory](live-operator-surface-inventory-2026-08-18.md)
observed this card and its “Account Clerk custody is healthy” dominant posture.

**Why it is not presentation.** The backend authors `guidance` and every
recovery capability, but it does not author the resulting posture disposition.
The browser is deciding whether the account is healthy, merely needs review,
must wait, or requires terminal escalation, and which action is attached to
that decision. That is exactly the safety/capability/primary-action boundary
ADR 0035 Decision 12 assigns to the backend.

**Adversarial check.** The same component is conformant when its `blocker`
input is populated: `blockerPosture` copies the ADR 0027 disposition, prose,
and primary move. The violation is limited to the `SqliteClerkProjection`
fallback. It is not legacy JSONL: the `authority_kind == "sqlite"` branch is
the branch that activates it.

### V2 — Per-bot banners derive the primary lifecycle action

**Severity:** medium. Action enablement and mutation revalidation remain
backend-owned, but the browser decides which command receives the dominant
banner placement in both lenses.

**Frontend derivation.** `primaryLifecycleAction` in
`Frontend/src/app/components/broker/v2-panel/bot-detail-banner/lifecycle-action.ts`
reads `health.running` and `health.desired_state`, selects an ordered action-id
set (`resume`, `continue`, or `stop`/`stop_bot_decisions`), and returns the first
matching `PanelAction`. Both `TraderBotBannerComponent` and
`OperatorBotBannerComponent` render that result in the banner's sole primary
action slot. `OperatorLensComponent` repeats the call to decide which readiness
action to suppress as already promoted.

**Reachability.** The registered route
`/brokers/:broker/accounts/:accountId/bots/:sid` loads `BotPanelShellComponent`,
which renders one of those two lenses. On an activated account,
`adapt_sqlite_panel` replaces custody actions with the recovery catalog and
authors `mission_verdict`, but `BotPanelView` carries no backend-authored banner
action reference. The browser therefore performs the final attention-routing
decision on every rendered bot panel. The live inventory observed Resume in
this slot on the stopped-bot state.

**Why it is not presentation.** Mapping a backend-selected action id to button
placement would be presentation. Choosing that id from health evidence is
remediation arbitration: it determines the one command the operator sees next
to the mission verdict. ADR 0035 Decision 12 explicitly names “which action is
primary” as backend-owned. The Gallery demonstrates the intended contract
shape by receiving a backend-authored `primary_action`; the detail panel does
not.

**Adversarial check.** `adapt_sqlite_panel` already preserves server-authored
action enablement, blocker prose, concurrency tokens, and recovery
`primary` flags. `OperatorReadinessComponent` consumes the recovery `primary`
flag rather than recomputing it. The remaining defect is narrowly the banner
action choice; tone selection and component placement after the backend chooses
an action remain legitimate presentation. The unmounted `PanelHeaderComponent`
also calls the helper, but has no production importer; it should be deleted or
converted with the live callers, not counted as another reachable seam.

### V3 — Account Strip derives trading and custody availability verdicts

**Severity:** medium. The strip does not enable a mutation, but it can announce
“Trading available” when the backend's actual paper-deploy readiness policy
would reject the account.

**Frontend derivation.** The live Account Strip template combines
`trading_blocked || account_blocked` into “Trading blocked” or “Trading
available.” That is not a rendering of one backend verdict: it omits the
backend's paper-mode and `account_status == ACTIVE` readiness requirements.
The same template combines `freeze.active` and `hold.active` into “Clear,” “New
entries blocked,” or “No custody block,” authoring another account-level
operational conclusion from separate facts.

**Reachability.** `BotsListPageComponent` mounts `app-account-strip` on the
registered Alpaca bot-roster route. These labels therefore appear in the live
Broker V2 product, not a retired compatibility route.

**Why it is not presentation.** The underlying booleans are backend facts, but
the user-facing availability answer is a conjunction with policy meaning. The
false-positive inactive/wrong-mode case proves that the browser is not merely
renaming one backend classification. ADR 0035 Decision 12 assigns that safety
and capability verdict to the backend.

**Remediation boundary.** This must not create a second account-posture
contract. Issue #1664 owns one backend-authored account operator view consumed
by both Account Desk and Account Strip: `dominant_blocker: OperatorBlocker |
null`, backend-authored status copy, and one action reference. Healthy is the
absence of a dominant blocker plus backend-authored status copy; review, wait,
fix-here, and terminal outcomes use ADR 0027's existing blocker disposition
instead of recreating the current five-state frontend taxonomy.

## Refuted or subtracted candidates

| Candidate | Classification and reachability result |
|---|---|
| Active deploy drawer | **Presentation/input validation.** `AlpacaPaperDeployView.eligibility`, readiness checks, execution-mode availability, and `allowed_actions` are backend-authored. Angular adds form validity and in-flight state; it does not author launch admission. The older `BrokerDeployFormComponent` and its TypeScript-built `OperatorBlocker`s have no production importer or registered route. |
| Bot mission verdict and readiness list | **Presentation.** `adapt_sqlite_panel` authors `mission_verdict`, recovery checks, ready/blocked counts, explanations, cures, and action capabilities at one SQLite revision. Angular renders those fields. |
| Roster and Gallery attention/status | **Presentation.** `adapt_sqlite_catalog` authors `needs_attention` and `status_explanation`; Gallery receives `primary_action` from the backend. Client filters, sorting, and transport `connecting`/`stale`/`error` states do not create a bot-safety verdict. |
| Global IBKR market-data banner | **Closed-enum presentation.** `/api/broker/health` runs every response through `_with_condition`, so current route responses carry backend-authored condition code, severity, title, summary, and remediation. Angular maps the closed connection state to layout/tone and renders condition copy. Its legacy fallbacks do not execute on the current router path. |
| Browser freshness and unavailable states | **Browser-local presentation.** Failed refresh with last-good data, SSE connection state, loading, and form-touched validation cannot be authored by a snapshot-producing backend because they describe the current browser request or interaction. They do not assert operational safety. |
| Account roster posture regex | **Unreachable.** `account-posture-tag-severity.ts` semantically interprets strings, but its only production caller is `AccountRosterPageComponent`; `/broker/accounts` and `/broker/accounts/:accountId` are redirect-only compatibility routes to `/brokers/alpaca`. |
| Generic custody-resolution receipt prose | **Legacy-only.** The active SQLite diagnosis makes the component render nothing, and the generic mutation endpoint rejects SQLite authority. ADR 0037 retires the remaining legacy JSONL path, so this resolves by deletion rather than a new verdict contract. |
| Broker Connectivity service's composed blocker strings | **Unreachable.** `BrokerConnectivityService` has no production consumer on the current tree. No live issue should be filed against an orphaned derivation without a caller. |
| Strategy, indicator, and research verdict components | **Out of scope.** They are analytic/research conclusions, not live bot-control or custody judgments. Their numeric authority is governed by the math provenance contract and their own product specifications. |

## Why no ADR is owed

The decision test is whether a downstream implementer still has to choose where
these judgments belong. They do not:

- account safety/capability/primary action belongs to the backend under ADR
  0035 Decision 12;
- blocker disposition and remediation belong to `OperatorBlocker` under ADR
  0027;
- numeric flatness belongs to ADR 0036;
- execution narratives belong to ADR 0014; and
- closed identifiers and generated manual copy remain the shared label rule and
  ADR 0041 respectively.

An ADR saying “the Frontend holds no non-numeric verdict” would restate those
accepted decisions at a looser scope, without resolving either code seam more
precisely. The correction is to make the account and bot-panel payloads conform.
This audit adds no vocabulary; per
[ADR 0040](../architecture/adrs/0040-context-glossary-scope-and-lineage-labels.md),
`Vocabulary: none owed` would have been the ADR line if an ADR were created.

## Filed issue 1 — [#1664](https://github.com/tim1016/learn-ai/issues/1664)

**Title:** `Alpaca account surfaces: backend-author one canonical operator posture`

### Problem

Two live account surfaces derive operator verdicts. Account Desk's
`projectionPosture` chooses `healthy | fix_here | wait | review | terminal` and
an attached recovery action from raw `ClerkProjectionResponse` fields. Account
Strip combines `trading_blocked || account_blocked` into “Trading available” or
“Trading blocked” and separately combines freeze/hold facts into custody-block
copy. Its available case is false for inactive or wrong-mode accounts because
the backend readiness policy also requires paper mode and `ACTIVE` status.
These violate ADR 0035 Decision 12 and bypass ADR 0027's backend-authored
disposition shape.

### Scope

- Extend the SQLite account projection with one canonical account operator view
  consumed by both Account Desk and Account Strip. Its contract is
  `dominant_blocker: OperatorBlocker | null`, backend-authored status copy, and
  one structured action/move reference. Do not add a parallel
  `healthy | fix_here | wait | review | terminal` enum.
- Represent review, wait, fix-here, and terminal cases with ADR 0027's existing
  blocker disposition from the same recovery-policy evidence cut that authors
  `guidance` and `recovery_actions`. Healthy is a null dominant blocker plus
  backend-authored status copy.
- Make `AlpacaOperatorPostureComponent` and Account Strip render that one result.
  Remove Account Desk's raw-evidence posture classification and copy; remove
  Account Strip's boolean-combination verdicts and local availability copy.
- Include paper mode and active account status in the backend-authored trading
  availability answer so it agrees with paper-deploy readiness.
- Preserve mutation-time rechecks and concurrency tokens unchanged.
- Regenerate the OpenAPI/Frontend contract and update the contract-drift gate.

### Acceptance

- Backend decision-table tests cover healthy, review-only uncertainty,
  available recovery, unavailable/waiting recovery, terminal authority
  failure, inactive account, and wrong execution mode.
- Component tests prove Account Desk and Account Strip render the same
  backend-authored status/disposition/action contract and malformed/missing
  posture fails closed rather than re-deriving from evidence.
- No client condition over `uncertainties`, `authority_health`, or
  `guidance.action_required`, nor a conjunction over account/freeze/hold flags,
  determines an operator availability verdict.

## Filed issue 2 — [#1665](https://github.com/tim1016/learn-ai/issues/1665)

**Title:** `Broker V2 panel: publish the primary banner action instead of deriving it in Angular`

### Problem

`primaryLifecycleAction` chooses `resume`, `continue`, or a stop action from
`BotPanelView.health`, and both live bot-detail lenses put that choice in the
banner's sole primary action slot. This is attention/remediation arbitration,
which ADR 0035 Decision 12 assigns to the backend. The backend already authors
the mission verdict, every action capability, and SQLite recovery `primary`
flags. The panel contract omits one canonical action reference, while readiness
separately exposes the generic `readiness_checks[].evidence.primary` marker.

### Scope

- Add one wire-level `primary_action_id` to `BotPanelView`, authored by the
  backend at the same revision as `mission_verdict` and `actions`. This field is
  the sole primary-action authority for routine lifecycle and SQLite recovery.
- Fold `RecoveryCapability.primary` into that selection and define/test the
  precedence. The selected id must reference an action present in the same
  payload. Remove `readiness_checks[].evidence.primary` as a presentation input;
  if retained as diagnostic evidence, enforce equality with
  `primary_action_id`.
- Make the Trader banner, Operator banner, and Operator readiness suppression
  consume only `primary_action_id`. Delete `primaryLifecycleAction` and the
  duplicate health-based selection. Delete the unmounted `PanelHeaderComponent`
  or convert it in the same change so an orphan cannot preserve the derivation.
- Keep visual tone and component layout frontend-owned after the backend has
  selected the action.
- Regenerate the OpenAPI/Frontend contract and update its drift checks.

### Acceptance

- Backend tests cover stopped/resumable, paused/continuable, running/stoppable,
  blocked, and recovery-primary snapshots, including exact agreement between
  any retained diagnostic recovery-primary marker and `primary_action_id`.
- A missing or inconsistent primary reference fails closed to no banner action;
  Angular does not fall back to health-based selection.
- Both lenses and readiness render/suppress the same backend-selected action
  for the same payload; no second primary-action marker can disagree.

## Registered `docs/known-gaps.md` insertion

The authoritative defect register is the
[“Non-numeric operator verdict ownership” section](../known-gaps.md#non-numeric-operator-verdict-ownership-verified-2026-08-18).
It records the corrected three-seam census and links the two filed fixes without
duplicating the register text in this point-in-time audit.
