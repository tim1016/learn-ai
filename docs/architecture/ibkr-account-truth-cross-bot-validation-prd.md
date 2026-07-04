# PRD: IBKR Account Truth and Cross-Bot Trade Validation

**Surface:** `/broker/account-monitor`, `/broker/reconciliation`, `/broker/orders`, and the per-bot Activity/Audit drill-downs that feed them.
**Builds on:** ADR 0008, ADR 0014, ADR 0016, `docs/ibkr-integration-authority.md`, `docs/references/ibkr-reconciliation.md`, `docs/runbooks/live-trade-reconciliation.md`, and the 2026-06-26 IBKR adapter-matrix auto-research run (pruned 2026-07-04; git history).
**Design source:** Codex IBKR API research on 2026-07-01, focused on TWS/Gateway, Flex Web Service, and Client Portal API trade/account surfaces.
**Data plane:** Python FastAPI broker adapter via `ib_async` and TWS/Gateway. Angular renders backend-authored verdicts and evidence. No GraphQL is introduced for this surface.
**Implementation-snapshot DoD:** update `docs/ibkr-integration-authority.md`, `docs/references/ibkr-reconciliation.md`, and `docs/runbooks/live-trade-reconciliation.md` when the shipped behavior changes.
**Status:** MVP implementation in progress on `codex/ibkr-cross-bot-validation-prd`.

**2026-07-01 implementation snapshot:** Slices 1-5 now have an MVP implementation in the PR branch: server-minted app manual order refs, what-if preview, completed-order sweep, account-truth projection, Account Monitor/Reconciliation rendering, and Orders ledger rendering. Follow-up slices remain for audited post-hoc manual adoption, Flex delayed audit import, Client Portal session-safety evaluation, operator/session-specific manual namespace attribution, and a durable commission-report event subscription.

---

## Problem Statement

Operators supervising multiple live-paper bots on the same IBKR paper account need one account-wide place to answer: **did every broker order, fill, commission, position, and P&L change come from a known bot, a known manual action, or an explicitly foreign source?**

Today the surfaces answer adjacent but incomplete questions:

- Account Monitor streams account and position P&L, but it does not classify exposure by bot ownership, manual ownership, or foreign ownership.
- Reconciliation is shaped for broker/engine comparison, but several IBKR deltas and per-fill comparisons are still placeholder or deferred.
- Orders is primarily a manual paper-order form and open-order viewer. It does not act as a cross-bot ledger of open, completed, rejected, cancelled, and filled orders.
- Per-bot Activity has the right truthfulness model: backend-authored rows, exact `order_ref` namespace matching, broker-authored row identity, and trader-readable narratives. But it is scoped to one bot, so it cannot prove the whole account is clean.

The missing product is an account truth board that joins IBKR account reality with all known bot namespaces. It should prove what is known, classify what is not known, and fail closed when account state cannot be trusted.

## Solution

Introduce a backend-owned **IBKR Account Truth** projection. The projection joins live TWS/Gateway facts with bot runtime evidence and emits account-level validation rows for Account Monitor, Reconciliation, and Orders.

TWS/Gateway remains the live authority. The first implementation uses `ib_async` primitives already available on the connected `IB` instance:

- `reqAllOpenOrders` or the existing open-order path for working orders.
- `reqCompletedOrdersAsync` for recently completed, cancelled, and rejected orders that no longer appear in open orders.
- `reqExecutionsAsync` for account-wide execution sweeps, including reconnect recovery.
- `commissionReportEvent` for durable fill fee evidence.
- `reqPositionsAsync`, account summary, `reqPnL`, and `reqPnLSingle` for positions, buying power, margin, and P&L evidence.
- `whatIfOrderAsync` for pre-submit margin/commission impact previews on manual paper orders.
- `errorEvent` and `reqCurrentTimeAsync` for broker liveness, clock, and failure evidence.

The ownership ladder is strict:

1. A bot-owned broker fact has an `order_ref` whose namespace exactly equals `learn-ai/{strategy_instance_id}/v1` for a known bot namespace. The namespace is the portion before the final colon.
2. A strong manual operator action is app-submitted and stamped before submit with a reserved `manual/{operator_or_session}/v1` namespace.
3. A broker fact with no recognized namespace is classified as `foreign_or_unclaimed` by default. Hand-clicked TWS orders, including client-id `0` rows with no namespace, must not be auto-classified as manual.
4. A weaker `adopted_manual` tier may be added by an explicit post-hoc adoption workflow. Adoption is an audited operator claim over raw broker facts, not a rewrite of broker identity.
5. `execId` is the idempotency key for executions. `permId` is the broker order grouping key. `orderId` is not durable enough to identify historical intent by itself.

Post-hoc adoption is a follow-up capability, not MVP classification. It exists because a single hand hedge in TWS would otherwise pin the account to `not_proven` forever. The design is explicit and append-only: the operator adopts one foreign/unclaimed fact at a time, keyed by durable identity (`permId` for an order, `execId` for an execution), with operator/session, `int64 ms UTC` timestamp, claimed manual namespace, optional reason, and correction records appended rather than mutated. Reclassification is derived by folding the adoption ledger over raw broker facts. A live working foreign order remains broker-foreign-by-identity with an operator claim overlaid; it needs stronger confirmation than a terminal historical execution because adoption can clear the unknown-open-order submit block. Broker `clientId` is decision support in the dialog, never machine ownership logic.

Page responsibilities:

- **Account Monitor** becomes the account truth and risk summary: account status, broker liveness, net exposure by ownership class, margin/buying-power buffers, open-order risk, and stale-data warnings.
- **Reconciliation** becomes the validation center: invariant verdicts across all known bots, manual orders, positions, executions, commissions, and delayed official Flex statements.
- **Orders** becomes the broker order ledger: open and completed orders grouped by `permId`, `order_ref`, ownership class, status, executions, commission, and broker evidence. Manual order placement remains available but moves behind explicit paper/manual controls and must stamp a manual namespace.
- **Per-bot Activity/Audit** stays the per-instance evidence trail. Account truth links to the specific Activity row, run artifact, or raw broker evidence when a row belongs to a bot.

Flex Web Service is the delayed official audit source. A later slice imports Flex statements to cross-check executions, commissions, cash, and positions after IBKR statement data settles. Client Portal API is optional and feature-flagged because `/iserver` brokerage-session behavior can interfere with other sessions; it is not part of the live validation path until session-safety is proven.

## User Stories

1. As an operator, I want one account-wide truth view, so that I can tell whether the IBKR paper account is clean before bots submit again.
2. As a trader, I want every IBKR execution assigned to a bot, a manual order, or a foreign/unclaimed bucket, so that unknown fills cannot hide inside account P&L.
3. As a trader, I want every open order grouped by owner and broker `permId`, so that I can see which bot or manual action is still working.
4. As a trader, I want completed, cancelled, rejected, and expired orders visible after they leave the open-order list, so that the Orders page does not forget broker history.
5. As a trader, I want commission evidence joined to each execution when IBKR reports it, so that per-fill P&L does not silently ignore fees.
6. As a trader, I want missing commission reported as a caveat, so that I know the fill row is not final yet.
7. As an operator, I want account positions reconciled against bot-owned positions, manual positions, and foreign/unclaimed positions, so that I can identify drift before the next submit.
8. As an operator, I want a clear verdict when the account is flat but old historical unknown executions exist before a known reset baseline, so that old noise does not block today unless it affects current risk.
9. As an operator, I want unknown open orders to be critical, so that no bot can submit while an unowned broker order is live.
10. As an operator, I want unknown current positions to be critical, so that the app fails closed when account exposure is not explained.
11. As a trader, I want manual paper orders stamped with a manual namespace, so that they are not misclassified as foreign.
12. As a trader, I want manual paper orders to show a what-if preview before submit, so that I can see approximate margin and buying-power impact.
13. As a trader, I want manual orders visually separated from bot-owned orders, so that I do not confuse operator tests with strategy behavior.
14. As an engineer, I want account truth computed in Python, so that Angular does not derive ownership, drift, fee status, or safety verdicts.
15. As an engineer, I want the frontend to render backend-authored labels and prose, so that raw broker/API codes do not leak into primary operator UI.
16. As an engineer, I want exact IBKR identifiers preserved in technical details, so that `execId`, `permId`, `orderId`, `conId`, and `order_ref` remain audit-ready.
17. As an engineer, I want all boundary timestamps to remain `int64 ms UTC`, so that account truth can be joined with live-run artifacts without local-time ambiguity.
18. As a trader, I want market/session times displayed in ET, so that broker events line up with U.S. market hours.
19. As an operator, I want broker liveness to include current-time and error-event evidence, so that stale or disconnected data is not mistaken for a clean account.
20. As an operator, I want account truth rows to show freshness, so that old positions, old order sweeps, or stale P&L ticks are visibly degraded.
21. As a trader, I want Account Monitor to show exposure by symbol and owner, so that I can see whether several bots are stacking risk in the same ticker.
22. As a trader, I want Account Monitor to show buying power, margin, and net liquidation value with freshness, so that account-level risk is visible before trades.
23. As an operator, I want Reconciliation to show invariant verdicts, so that I know which proof failed instead of reading raw tables.
24. As an operator, I want a verdict for `all_executions_assigned`, so that any unowned execution is immediately visible.
25. As an operator, I want a verdict for `positions_match_known_ownership`, so that current exposure is proved against bot/manual ownership.
26. As an operator, I want a verdict for `open_orders_known`, so that working broker risk is fully attributed.
27. As an operator, I want a verdict for `completed_orders_known`, so that terminal broker outcomes are accounted for.
28. As an operator, I want a verdict for `commission_complete`, so that fee-delayed rows are visible without being overclassified.
29. As an operator, I want a verdict for `broker_liveness_proven`, so that reconciliation cannot pass on a dead broker session.
30. As an operator, I want a verdict for `flex_audit_match` after statement import, so that delayed official IBKR statements can confirm the live projection.
31. As an operator, I want Client Portal cross-checks to be clearly labelled experimental or disabled, so that I do not depend on a surface that can disturb the brokerage session.
32. As a trader, I want the Orders ledger to link a broker order to its executions and commissions, so that I can inspect a full lifecycle from one row.
33. As a trader, I want a bot-owned ledger row to link back to the bot's Activity/Audit detail, so that I can see the engine intent and narrative behind it.
34. As an operator, I want duplicate execution redeliveries suppressed by `execId`, so that reconnect sweeps do not create fake extra fills.
35. As an operator, I want reconnect-recovered executions labelled, so that observation lag is not confused with execution lag.
36. As an engineer, I want fake-IBKR tests for completed orders, execution sweeps, commission events, what-if previews, and broker errors, so that CI does not need a live Gateway.
37. As an engineer, I want deterministic fixtures for multi-bot, manual, and foreign broker facts, so that account-truth classification is regression-tested.
38. As a reviewer, I want the PRD sliced into independent vertical agent tasks, so that Orders repair, endpoint capture, account projection, UI wiring, and Flex import can ship separately.
39. As an operator, I want a degraded but readable state when IBKR does not return one evidence source, so that partial outages do not produce false confidence.
40. As a trader, I want final account truth to say "not proven" instead of "clean" whenever required broker evidence is stale, missing, or contradictory.
41. As an operator, I want TWS-clicked orders with no namespace to default to `foreign_or_unclaimed`, so that the system does not invent manual ownership without evidence.
42. As an operator, I want to adopt one foreign/unclaimed order or execution as manual after review, so that a deliberate hand hedge does not permanently block every bot.
43. As an operator, I want adopted-manual rows distinguished from app-minted manual rows, so that weaker post-hoc claims are visible.
44. As an auditor, I want adoption decisions stored in an append-only ledger keyed by `permId` or `execId`, so that corrections are traceable and no broker fact is rewritten.
45. As a trader, I want live working foreign-order adoption to require stronger confirmation than adopting terminal history, so that clearing the bot-submit block is deliberate.

## Implementation Decisions

1. **Backend-owned projection.** Add an account-level projection in Python that consumes TWS/Gateway facts and live-run/bot evidence, then emits backend-authored rows, labels, verdicts, and drill-down facts.
2. **TWS-first live path.** Use the TWS API through `ib_async` for live account truth. Do not make Client Portal `/iserver` calls from the live validation path.
3. **Add missing broker primitives.** Expose curated FastAPI endpoints or service functions for completed orders, execution sweeps, commission reports, what-if previews, broker current time, and broker error evidence.
4. **Do not bypass existing Activity truth.** Per-bot Activity remains the per-execution operator narrative for a bot. Account truth references it when available and classifies gaps when not available.
5. **Ownership is exact namespace equality.** Never use prefix matching for `order_ref`. Cross-version support must be an explicit allowed-namespace set.
6. **Unknown defaults to foreign.** Missing namespace, client-id `0`, TWS hand clicks, other API sessions, and unparseable ownership evidence all default to `foreign_or_unclaimed`. Do not infer manual from client id, account id, timing, or absence of a namespace.
7. **Manual orders get a namespace.** The Orders page must stop submitting orders without `order_ref`. Manual paper submits use a reserved manual namespace and explicit paper confirmation.
8. **Adoption is audited overlay evidence.** A future post-hoc adoption workflow may classify one raw broker fact at a time as `adopted_manual`, but it must append to an adoption ledger and never mutate raw broker evidence or silently mark broker-foreign identity as safe.
9. **Manual evidence tiers stay visible.** `app_minted_manual` and `adopted_manual` share the manual owner class for rollups, but UI and drill-downs must preserve the stronger/weaker evidence tier distinction.
10. **Order grouping uses broker identifiers carefully.** `permId` groups a broker order lifecycle, `execId` dedupes fills, and `order_ref` assigns ownership. `orderId` is displayed as evidence but is not the account-truth identity key.
11. **Invariant verdicts are closed.** Initial invariant keys are `all_executions_assigned`, `positions_match_known_ownership`, `open_orders_known`, `completed_orders_known`, `commission_complete`, `broker_liveness_proven`, `flex_audit_match`, and `client_portal_cross_check_optional`.
12. **Account Monitor is risk-first.** It shows liveness, buying power, margin, net liquidation, owner-class exposure, symbol exposure, and stale evidence. It does not become a raw IBKR dump.
13. **Reconciliation is proof-first.** It shows verdicts, blockers, caveats, and links to evidence. It is the primary place to decide whether the account is safe for further bot submits.
14. **Orders is ledger-first.** It shows order lifecycle and evidence across open and completed orders. Manual submit is secondary and guarded.
15. **Flex is delayed official audit.** Flex import is a follow-up slice that validates settled executions, commissions, cash, and positions. It may lag live broker truth and must be labelled as delayed.
16. **Client Portal is optional.** Client Portal endpoints may be explored behind a feature flag only after documenting session interaction risks. The first implementation does not depend on it.
17. **Timestamp policy is unchanged.** Wire/storage fields remain `int64 ms UTC`; Angular formats display time at the edge.
18. **Trader copy is backend-authored.** Angular may format, sort, filter, expand, and render. It must not compute ownership, verdict severity, risk posture, or reconciliation pass/fail.

## Suggested Agent Slices

1. **Repair manual Orders submit.** Ensure frontend/manual order requests provide `order_ref`, add manual namespace support, and add a what-if preview before paper submit.
2. **Add TWS evidence capture.** Implement and test completed-order sweeps, execution sweeps, commission report capture, current-time checks, and error-event recording in the Python broker layer.
3. **Build account truth projection.** Join IBKR facts with known bot namespaces and manual namespaces. Emit invariant verdicts, owner classes, freshness, and drill-down evidence.
4. **Wire Account Monitor and Reconciliation.** Render account truth summary, invariant verdicts, owner-class exposure, stale evidence, and links back to per-bot Activity.
5. **Upgrade Orders ledger.** Show open and completed broker orders grouped by lifecycle, ownership, executions, commission, and status. Keep manual submit secondary.
6. **Add post-hoc manual adoption.** Add an append-only adoption ledger and one-fact-at-a-time operator workflow for foreign/unclaimed orders or executions. Fold the ledger over account truth, keep `adopted_manual` distinct from app-minted manual, and require stronger confirmation for live working orders.
7. **Add Flex audit import.** Import official Flex statement data and reconcile settled executions, commissions, cash, and positions against the live projection.
8. **Evaluate Client Portal cross-check.** Document session-safety findings before enabling any CP endpoint in operator surfaces.

## Testing Decisions

- **Broker layer tests:** use fake `ib_async` clients for `reqCompletedOrdersAsync`, `reqExecutionsAsync`, `whatIfOrderAsync`, `commissionReportEvent`, `errorEvent`, and current-time checks. CI must not require a live IBKR Gateway.
- **Projection tests:** cover multi-bot ownership, manual ownership, foreign executions, unknown open orders, duplicate `execId`, partial fills, missing commission, stale evidence, reconnect sweeps, and current-position drift.
- **Adoption tests:** cover default foreign/unclaimed classification for unstamped TWS clicks, one-fact adoption keyed by `permId` or `execId`, adopted-manual evidence tier rendering, correction-by-append semantics, and stronger confirmation for live working order adoption.
- **Orders tests:** prove manual submits include an `order_ref`, paper confirmation remains required, what-if failures block submit unless explicitly degraded by backend policy, and completed orders remain visible after they leave open orders.
- **Frontend tests:** assert rendered trader labels, invariant verdicts, degraded states, owner-class grouping, links to Activity/Audit, and that raw backend codes in primary UI pass through the shared `receiptLabel` path where applicable.
- **Timestamp tests:** contract-test that new wire models expose milliseconds, not ISO strings, for event time, execution time, sweep time, current broker time, and freshness.
- **Narrative tests:** every invariant and ownership class has backend-authored operator copy plus a diagnostic fallback for unmapped broker evidence.
- **No live-broker tests in CI:** live Gateway checks belong in a manual runbook or local smoke script, not the default test suite.

## Out of Scope

- Real-money live trading.
- Changing bot strategy execution, sizing, or signal logic.
- Replacing the per-bot Activity/Audit model.
- GraphQL transport for these surfaces.
- Frontend-computed reconciliation math, ownership, or verdicts.
- Auto-classifying TWS hand-clicks, client-id `0` orders, or unstamped broker facts as manual.
- Client Portal brokerage-session use in the live validation path.
- Portfolio optimization or new P&L math beyond displaying broker/account evidence and backend-authored comparisons.

## Further Notes

- Official IBKR surfaces reviewed: TWS API, TWS P&L/account/order/execution callbacks, Client Portal API v1, and Flex Web Service.
- The researched recommendation is conservative: TWS/Gateway is the live source, Flex is the delayed statement audit, and Client Portal is optional until its session behavior is proven safe for this operator workflow.
- This work should preserve the repo's current truthfulness contract: broker facts are captured once, backend code authors the meaning, and the frontend does not invent safety claims from raw fields.
