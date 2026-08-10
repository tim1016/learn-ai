# PRD — UI-driven Alpaca SQLite paper acceptance and IBKR control retirement

- **Date:** 2026-08-10
- **Status:** Executed — acceptance rubric passed on 2026-08-10
- **Operator:** Inkant Awasthi
- **Primary product:** Alpaca Broker V2
- **Execution boundary:** Alpaca paper only; one share; live-money remains disabled
- **Operational interface:** Product UI only
- **Architecture:** Event-sourced SQLite remains the sole activated Alpaca custody
  authority; this PRD changes the acceptance evidence, not the implementation design
- **Related:** ADR 0035, #1409, #1411, #1413, #1416, PR #1438

## 1. Executive summary

The Alpaca Account Clerk is already implemented as a production-grade,
activation-selected, event-sourced SQLite authority. A hash-chained append-only
transition log owns custody; folded current-state tables are updated in the same
transaction; SQL constraints own idempotency; missing or invalid activated authority
fails closed; and verified backups plus the finalized append mirror provide recovery.

The remaining decision is whether a same-day, real Alpaca-paper round trip from the
canonical `deployment_validation` strategy provides enough live evidence to accept the
migration. The result is not predeclared. The operator will drive the entire ceremony
through Alpaca Broker V2. A qualifying ENTER and EXIT must be strategy-owned and must
traverse the visible SQLite command, effect, broker-order, fill, position, terminal,
and reconciliation chain. Existing deterministic/adversarial tests and verified
backup evidence remain supporting proof for failure paths that one normal trade cannot
exercise.

If the combined evidence proves the acceptance invariants, ADR 0035 may be accepted
for Alpaca paper and the migration issues may close. If a material invariant remains
ambiguous, the issues remain open and only the specific missing proof is restored as a
requirement. The earlier multi-session matrix is retained as historical governance and
may become post-acceptance hardening; it is not silently rewritten as completed.

After the paper bot is stopped, reconciled, flat, and order-free, a separate retirement
slice removes the deprecated IBKR bot-control and navigation product surfaces. IBKR
compatibility URLs may remain only as redirects to Alpaca Broker V2. The IBKR
market-data adapter is not removed in this slice because today's deployment-validation
runtime currently uses it as a feed dependency; product control authority remains
Alpaca-only.

## 2. Decisions

1. **Engineering quality is unchanged.** No reduced SQLite implementation, temporary
   storage mode, direct database edit, dual writer, or legacy fallback is permitted.
2. **Acceptance is evidence-driven.** One complete paper round trip is the candidate
   live anchor, not an automatic pass.
3. **All operational actions use the UI.** Deployment, Start/Resume, evidence
   inspection, Stop, safe flatten if presented, and reconciliation are performed only
   through Alpaca Broker V2.
4. **No hidden transport bypass.** Browser automation may click and read the rendered
   product, but may not issue direct HTTP requests, invoke internal JavaScript services,
   call a trading CLI, or open SQLite.
5. **Backend-authored capability governs.** If the UI disables or refuses an action,
   automation does not work around it. The refusal becomes evidence.
6. **A manual broker trade never qualifies.** The ENTER and EXIT must originate from
   `deployment_validation` and retain the same strategy/run/effect identity chain.
7. **Risk is bounded to one paper share.** The UI ticket uses `safe_canary`, quantity
   `1`, symbol `SPY`, execution mode `paper`, and carryover forbidden.
8. **Live-money remains disabled.** Nothing in this PRD authorizes live trading.
9. **IBKR broker control is retired, not migrated.** Deprecated IBKR control UI and its
   supporting product projections are removed after the session; compatibility routes
   redirect to Alpaca.
10. **IBKR feed removal is separate.** The read-only market-data adapter remains only
    as an internal feed dependency for today's strategy. Replacing that feed is a
    separately scoped migration and cannot restore IBKR product control.

## 3. Goals

1. Prove through the user-facing product that the activated Alpaca paper account is
   governed by SQLite and not by legacy JSONL custody authority.
2. Deploy or select one immutable `deployment_validation` strategy instance through
   the canonical Alpaca deploy UI.
3. Observe one strategy-owned ENTER fill at a real Alpaca paper price.
4. Observe the canonical strategy-owned EXIT after three subsequent closed bars.
5. Prove that SQLite attributes the fill, folds the position, reaches
   `EXIT_ATTRIBUTED_FLAT`, and reconciles cleanly with Alpaca.
6. Preserve a complete UI-visible receipt package sufficient for an independent
   reviewer to follow the causal chain.
7. Decide acceptance honestly from the evidence rather than from the desire to finish
   today.
8. Remove the deprecated IBKR bot-control/navigation product after the account returns
   to a safe terminal state.

## 4. Non-goals

- Live-money enablement.
- Direct API, CLI, database, filesystem, or broker-console control of the paper
  ceremony.
- Manual ENTER or EXIT counted as deployment-validation evidence.
- Rewriting historical failed attempts as passes.
- Claiming that one normal round trip exercises every injected race or corruption
  condition.
- Removing the IBKR market-data adapter before a replacement feed is selected and
  validated.
- Developing, restoring, or expanding any deprecated IBKR bot-control surface.

## 5. UI-only automation contract

The browser controller operates only on visible, interactive UI state.

- Re-read the page immediately before every mutating click.
- Resolve controls by their accessible label and surrounding bot/account identity,
  never by stale DOM position.
- Before confirmation, verify the rendered broker is Alpaca, mode is Paper, account is
  `PA3KWXU1C4C3`, symbol is `SPY`, quantity is `1`, and strategy is
  `deployment_validation`.
- After every mutating click, wait for the product-authored receipt and verify its
  action, account, strategy instance, and lifecycle run before continuing.
- A read-only evidence interaction must never create a lifecycle receipt. If it does,
  classify the attempt as a custody abort.
- Browser reload is allowed; it must reconstruct the same backend-authored state.
- No browser-evaluated `fetch`, direct service invocation, cookie inspection, local
  storage inspection, or authentication extraction is allowed.
- Screenshots may support human review, but durable UI receipts and evidence references
  are the acceptance record.

## 6. Canonical UI surfaces

| Purpose | Route | Required observations/actions |
| --- | --- | --- |
| Account preflight and final proof | `/brokers/alpaca` | Paper account identity, connection posture, SQLite custody/authority presentation, positions, working orders, holds/uncertainties, account reconciliation |
| Account bot roster | `/brokers/alpaca/accounts/PA3KWXU1C4C3/bots` | Exact strategy instance, lifecycle state, exposure, working/uncertain counts, safe available action |
| Deployment | `/brokers/alpaca/accounts/PA3KWXU1C4C3/deploy` | `deployment_validation`, SPY, `safe_canary`, one share, paper, carryover forbidden, authored deploy/start decision and receipt |
| Bot control and evidence | `/brokers/alpaca/accounts/PA3KWXU1C4C3/bots/<strategy_instance_id>` | Trader and Operator lenses, market/feed posture, decisions, commands, effect operations, broker orders, fills, transaction evidence, Stop and reconciliation |

Deprecated `/broker/...` and IBKR bot-control routes are never used for the ceremony.

## 7. Preconditions

All must be visible through the product before deployment or Resume:

- Alpaca account `PA3KWXU1C4C3` is explicitly Paper.
- The product presents the selected Alpaca SQLite authority as healthy and available.
- No account-wide hold, uncertainty, mixed-writer, recovery, or stale-evidence blocker
  is present.
- Alpaca positions are empty and open/working orders are zero.
- Market/feed state is advancing and the bot has a current closed bar.
- No governed Alpaca bot is unexpectedly active.
- The selected strategy is `deployment_validation`, signal/trade symbol is `SPY`, and
  sizing is the one-share safe canary.
- The deployment action is backend-authored as available. A disabled action blocks the
  ceremony until its stated cause is resolved through supported UI behavior.
- The browser controller and operator agree on the exact strategy instance ID before
  the first mutation.

## 8. Same-day paper scenario

### Phase A — account and UI preflight

1. Open the Alpaca Account Desk.
2. Record the rendered account mode, authority/custody posture, connection/feed
   posture, position count, open-order count, holds, and uncertainties.
3. Open the account bot roster and confirm no unexpected active instance or exposure.
4. If a prior validation instance is immutable, correctly configured, and cleanly
   stopped, it may be resumed. Otherwise use Deploy to create
   `sqlite-ui-accept-0810` with the locked ticket in §7.
5. Review the authored deploy/Start decision before confirmation.

### Phase B — deploy or Resume through the UI

1. Click the single authored Deploy/Start or Resume action.
2. Verify the returned receipt names the expected account, strategy instance,
   lifecycle run, execution mode `paper`, and one-share sizing.
3. Navigate to the canonical bot panel from the receipt or roster.
4. Confirm the bot is on duty, the feed advances, and no order exists before a
   strategy ENTER decision.

### Phase C — observe the strategy-owned ENTER

The canonical kernel emits ENTER after two consecutive green closed one-minute bars,
starting after 09:45 America/New_York.

1. Observe decision receipts until ENTER or until the session must abort.
2. For ENTER, capture the visible chain:
   strategy decision → SQLite command → effect operation → client/broker order → fill.
3. Record source, observation, and durable timestamps where the UI exposes them.
4. Confirm the fill price is real Alpaca paper evidence, quantity is exactly one share,
   the order identity is linked to the effect, and attributed exposure becomes one SPY
   share for this bot.
5. Confirm no duplicate command, effect, order, or economic fill appears.

### Phase D — observe the strategy-owned EXIT

The canonical kernel emits EXIT after three subsequent closed bars while the cycle is
active, or at the documented session stop.

1. Do not manually sell or use a generic flatten to manufacture the expected result.
2. Capture the visible EXIT chain using the same identities and clock fields as ENTER.
3. Confirm the sell fill closes exactly the Clerk-attributed one-share remainder.
4. Confirm the durable terminal state reaches `EXIT_ATTRIBUTED_FLAT`.

### Phase E — safe terminal proof

1. Use the backend-authored Stop action in the UI after EXIT is terminal.
2. Verify the Stop receipt belongs to the expected strategy instance and run.
3. Use the UI's Reconcile action.
4. Confirm the account and bot both show flat exposure, zero open/working orders, zero
   unresolved operations, zero active uncertainty, and clean reconciliation.
5. Reload the bot panel. Confirm it reconstructs the same stopped-flat state and does
   not restart automatically.
6. Open multiple read-only evidence stations and raw-evidence views. Confirm none
   dispatches a lifecycle action and all preserve the same stopped-flat truth.
7. Return to the Alpaca Account Desk and record the final paper account proof.

## 9. Evidence package

The final record must contain or link:

- exact account, strategy instance, lifecycle run, symbol, sizing, and execution mode;
- deploy/Start or Resume receipt;
- advancing-bar and decision evidence before ENTER;
- ENTER command, effect operation, order reference, broker order, fill price/quantity,
  and three clocks where presented;
- SQLite-attributed post-ENTER position;
- EXIT command, effect operation, order reference, broker order, fill price/quantity,
  and three clocks where presented;
- `EXIT_ATTRIBUTED_FLAT`, Stop, and reconciliation receipts;
- final account and bot position/open-order/uncertainty proof;
- browser reload and read-only evidence-interaction observations;
- any warning, disabled action, discrepancy, retry, or abort as a separate attempt;
- the existing deterministic/adversarial qualification artifacts, focused authority
  tests, and verified generation-1 backup as supporting architecture evidence.

## 10. Acceptance rubric

The round trip is sufficient only if every statement below is supported without
inference from a missing UI fact:

- [x] The product proves Alpaca Paper and the intended account.
- [x] The product proves the activated SQLite custody path; no legacy fallback or
      mixed authority appears.
- [x] Deployment/Resume creates exactly one expected lifecycle run.
- [x] A strategy decision creates exactly one durable ENTER command/effect before
      exactly one broker order and real-price fill.
- [x] SQLite attribution and the broker-observed one-share position agree after ENTER.
- [x] The canonical strategy creates exactly one durable EXIT command/effect and the
      expected closing fill.
- [x] The terminal SQLite fold, bot view, and Alpaca account all agree on flat exposure.
- [x] Stop and reconciliation complete with zero working orders and zero unresolved
      custody.
- [x] Browser reload and evidence inspection preserve state and cause no mutation.
- [x] Existing adversarial, recovery, idempotency, no-fallback, and backup evidence is
      linked and remains valid for the deployed build.
- [x] An independent review finds no unexplained identity, clock, order, position, or
      authority gap.

If all boxes pass, accept ADR 0035 for Alpaca paper. If any box lacks proof, do not
reinterpret it as pass: retain the open issues and specify the smallest additional
scenario needed to establish that fact.

## 11. Abort conditions

Immediately stop progressing through the ceremony if any occurs:

- account mode is not explicitly Paper;
- the account, bot, strategy, symbol, or quantity differs from the locked ticket;
- feed does not advance or durable decision evidence stays stale;
- a UI evidence control emits a lifecycle action;
- receipt action/identity differs from the clicked action/visible target;
- duplicate command, effect, order, or fill appears;
- broker contact lacks a prior durable SQLite command/effect reference;
- an unexpected position, open order, hold, uncertainty, or account drift appears;
- EXIT cannot prove the attributed remainder or terminal flatness;
- Stop or reconciliation is refused or remains unresolved;
- the UI presents contradictory account, bot, or timeline truth;
- authority integrity, identity, recovery, or mixed-writer posture is not clean.

On abort, use only the safe UI actions the backend presents. Preserve receipts and
return to the Account Desk. Never bypass a guard. An emergency UI flatten protects the
paper account but does not count as a qualifying strategy EXIT.

## 12. Acceptance and issue disposition

After the evidence package is independently reviewed:

1. Update the soak report with the attempt and final verdict.
2. If the rubric passes, change ADR 0035 from Proposed to Accepted for Alpaca paper
   only; retain exact Alpaca-only supersession language and the live-money prohibition.
3. Close #1411 only if the advancing live-feed evidence supports its amended live
   requalification bar.
4. Close #1413 only if the stopped-flat reload and evidence-interaction observations,
   combined with the existing recovery/root-cause evidence, support the amended bar.
5. Close #1409 after the ADR acceptance change merges and its remaining items are
   reconciled against the amended rubric.
6. Close #1416 after the campaign outcome and operator decision are recorded.
7. Move any unrequired multi-session/fault evidence to post-acceptance hardening
   issue [#1440](https://github.com/tim1016/learn-ai/issues/1440). Do not mark those
   historical rows completed if they were not run.

## 13. Deprecated IBKR broker-control retirement

This work starts only after the Alpaca paper account is stopped, reconciled, flat, and
order-free. It is removal/decommissioning work permitted by the repository's IBKR
deprecation policy.

### 13.1 Frontend

- Remove the deprecated `Interactive Broker` sidebar group and all remaining links to
  its former product pages.
- Delete or retire the legacy bot list/control components under
  `Frontend/src/app/components/broker/bots/` and
  `Frontend/src/app/components/broker/bot-control/`.
- Keep the retired `/broker...` URLs only as redirect stubs to the appropriate Alpaca
  Broker V2 surface. They must not attach a component, provider, guard, or API behavior.
- Remove tests and fixtures that assert live IBKR bot-control behavior; replace them
  with redirect/absence tests.
- Preserve non-control IBKR documentation only where it describes an internal data-feed
  dependency or historical archived design.

### 13.2 Python and contracts

- Remove the deprecated IBKR bot catalog/control projections from
  `PythonDataService/app/routers/live_instances.py` and their supporting surface
  assemblers.
- Remove routes/contracts used solely by the retired IBKR bot-control UI.
- Preserve shared infrastructure only when an active Alpaca Broker V2 consumer exists;
  rename or relocate misleading IBKR-owned abstractions when necessary.
- Regenerate OpenAPI and frontend types after contract removal.
- Add contract guards proving retired routes and schemas cannot return.

### 13.3 Documentation

- Update active navigation, operator manuals, and architecture maps so Alpaca Broker V2
  is the sole broker-control product.
- Preserve historical ADRs/audits as history; add supersession notes rather than
  rewriting past evidence.
- The auto-generated README feature inventory may continue to describe physical legacy
  code only until that code is deleted; after deletion, run the normal feature sync.

### 13.4 Retirement acceptance

- [x] No Interactive Broker sidebar group or live product link remains.
- [x] Every retired `/broker...` compatibility URL redirects to Alpaca and renders no
      IBKR component.
- [x] Deprecated bot list/control Angular components are deleted.
- [x] Deprecated Python catalog/control projection code is deleted.
- [x] OpenAPI and generated frontend types contain no retired control contract.
- [x] Frontend, Python, and contract guard suites pass.
- [x] The Alpaca Broker V2 desk, deploy workflow, bots list, and bot panel remain green.
- [x] The internal market-data dependency, if still required, is documented as feed-only
      and has no broker-control navigation or mutation surface.

## 14. Same-day sequence

1. Review and approve this PRD and the evidence-driven acceptance amendment.
2. Ensure the UI is signed in and open the Alpaca Account Desk.
3. Run Phases A–E through the UI.
4. Review the evidence package against §10.
5. If sufficient, publish the soak/ADR acceptance change and close eligible issues.
6. With the paper account safely terminal, begin the IBKR control-retirement slice.
7. If insufficient, leave acceptance open and add only the targeted missing proof.

## 15. Definition of done

The migration is accepted when the event-sourced SQLite architecture remains intact,
the UI-driven paper evidence satisfies §10, ADR 0035 is Accepted for Alpaca paper, and
the issue record honestly reflects both completed proof and residual hardening.

The broker-control product cleanup is complete when Alpaca Broker V2 is the only live
broker-control experience and deprecated IBKR control code has been removed or reduced
to explicit redirect-only compatibility stubs. Live-money remains disabled.

## 16. Execution record — 2026-08-10

The ceremony ran entirely through the rendered Alpaca Broker V2 UI on paper account
`PA3KWXU1C4C3`. Preflight showed Account Clerk generation `1` healthy, no hold or
uncertainty, no positions, no working order identity, and three existing bots all
off-duty and flat. A fresh immutable bot was deployed because the prior candidate did
not expose enough rendered sizing/carryover proof.

| Evidence | Observed result |
| --- | --- |
| Bot/run | `sqlite-ui-accept-0810`; lifecycle run `36eef5961dfa4c6697d3109a065d9742`; Deployment Validation; SPY; Paper; one share; carryover forbidden |
| Deploy | Receipt `alpaca-paper-deploy:PA3KWXU1C4C3:sqlite-ui-accept-0810:1786384614022`; on duty with live advancing feed and zero pre-ENTER orders |
| ENTER | Decision/bar `12:58:06`/`12:58:00` CT; effect `effect:sqlite-ui-accept-0810:encoded-MTc4NjM4NDY4MDAwMDpFTlRFUg`; client order `learn-ai/sqlite-ui-accept-0810/v1:KCq7OE8DSe-UgX5Y1kLaBQ`; Alpaca order `bae47099-fc66-4da9-8de9-c34de9ada8a5`; BUY 1 SPY filled at `$772.74` at `12:58:07` CT |
| Post-ENTER | Bot-attributed and Alpaca-account exposure both showed exactly one SPY share at average `$772.74`; no duplicate effect, order, or fill |
| EXIT | Third-subsequent-bar decision/bar `13:01:05`/`13:01:00` CT; effect `effect:sqlite-ui-accept-0810:encoded-MTc4NjM4NDg2MDAwMDpFWElU`; client order `learn-ai/sqlite-ui-accept-0810/v1:t5WWgHc6aoonBNHQCrcrdA`; Alpaca order `b513fd2f-28ee-4d65-9f73-a3956d8f616a`; SELL 1 SPY filled at `$772.83` at `13:01:06` CT |
| Terminal | `EXIT_ATTRIBUTED_FLAT` rendered as **Attributed exposure flat**, followed by **Reconciliation completed** at `13:01:13` CT; zero positions and working orders |
| Stop/reconcile | Stop receipt `cmd:PA3KWXU1C4C3:sqlite-ui-accept-0810:36eef5961dfa4c6697d3109a065d9742:STOP:STOPPED` at `13:02:29` CT; final reconciliation receipt `reconciliation:345` at `13:02:47` CT |
| Reconstruction | Reload remained Off duty / Runtime idle / Stopped flat. Signal raw evidence, Audit trail, and Run evidence opened without action; journal remained exactly `18` events and the run did not restart. |
| Final account | Paper; Clerk healthy generation `1`; no position, working order identity, hold, uncertainty, or freeze; reconciliation clean. |

The post-run review found one presentation defect: SQLite intentionally reports
`fills_today = null` because the active custody folds do not provide the legacy fill
rollup, but the Trader lens simultaneously said **No fills today** despite the visible
broker fill and one-share exposure. A regression was written first; the component now
distinguishes unavailable history, a verified zero count, and fills outside the chart
window. Fourteen focused Trader tests pass, and live reload now renders **Fill history
unavailable from active custody folds** without the false zero claim. The stopped-flat
run still had `18` events after this evidence-only interaction.

Supporting authority evidence remains the verified generation-1 online backup at
`1786381437981` ms UTC and the `82` passing activation/no-fallback/cutover/schema/
retirement tests enumerated in the soak report. Redirect/sidebar validation passed
`25` frontend tests. The deprecated IBKR bot-list/control components and Python
surface assemblers had already been deleted in the retirement commits; current tests
prove the compatibility URLs are redirect-only and retired contracts cannot return.
No backend contract changed during this final UI correction, so no additional OpenAPI
or generated-type delta was required.

**Decision:** accept ADR 0035 for Alpaca paper only. Live-money remains disabled. The
historical multi-session injected-fault matrix is not marked complete; it moves to
[post-acceptance hardening issue #1440](https://github.com/tim1016/learn-ai/issues/1440)
rather than remaining an unbounded acceptance blocker.
