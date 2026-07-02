# IBKR adapter matrix — 2026-06-26

**Slice:** full IBKR adapter matrix after fixing F-0037 through F-0039.  
**Git base:** PR #690 merge commit `2f86597d`; fixes on `codex/fix-ibkr-activity-evidence`.  
**Scope:** `PythonDataService/app/broker/ibkr/*`, broker/live-instance routers, Bot Control Activity/Audit/Status/Configuration surfaces.

## Legend

- **Raw evidence** means `IbkrApiEvidenceEvent` in `PythonDataService/app/broker/ibkr/api_evidence.py`, exposed by `GET /api/broker/ibkr/evidence` and `GET /api/broker/ibkr/evidence/stream`.
- **Normalized schema** means a typed model in `PythonDataService/app/broker/ibkr/models.py` or the Activity projection models in `PythonDataService/app/schemas/live_runs.py`.
- **Persistence** means durable/parquet/jsonl artifacts. The raw evidence stream itself is in-process bounded diagnostics, not durable storage.
- **UI display** distinguishes the general Audit-tab raw evidence panel from domain-specific trader surfaces.

## Request/callback matrix

| IBKR request / callback | Adapter entry point | Raw evidence | Normalized schema | Persistence | API surface | UI display | Status |
|---|---|---|---|---|---|---|---|
| `accountSummaryAsync` / `accountSummary` | `account.fetch_account_summary` | Captured with account id and raw row objects | `IbkrAccountSummary` | `make_account_writer` can persist account snapshots when stream/router caller wires it | `GET /api/broker/account`; bot control account summary poll | Bot Control account banner/summary; Audit raw evidence panel; generic Activity evidence row when session evidence is in range | Displayed, raw detail available |
| `reqPositionsAsync` / `position` | `account.fetch_positions` | Captured with position objects | `IbkrPositionsSnapshot`; Activity position-lifecycle annotations derive from fills, not positions | Account writer can persist snapshots; Activity uses broker WAL for fills | `GET /api/broker/positions`; live-instance status/current risk; Activity projection | Status/Risk owned positions; Activity broker evidence row; Audit raw evidence panel | Displayed, raw detail available |
| `qualifyContractsAsync` / `contractDetails` for stocks/options | `contracts.qualify_stock`, `contracts.qualify_option`, option drill-down helpers, order preflight qualification | Captured in several qualification paths | Qualified contract-derived search/contract models | Usually request-scoped/in-memory; no dedicated durable qualification artifact | Broker option contract/search endpoints; order placement preflight | Deploy option picker / option-chain workflows; Audit raw evidence panel | Displayed through downstream domain surfaces; raw detail available |
| `reqSecDefOptParamsAsync` / `securityDefinitionOptionParameter` | `contracts.fetch_option_expirations`, strike/chain helpers | Captured with option parameter objects | `IbkrStrikeList` and option-chain helper outputs | Request-scoped/in-memory | `GET /api/broker/expirations/{symbol}` and option-chain helpers | Deploy/option picker and chain flows; Audit raw evidence panel | Displayed through option-selection surfaces; raw detail available |
| `reqMatchingSymbolsAsync` / `symbolSamples` | `symbol_search.search_symbols` | Captured with pattern and security-type filter | `SymbolMatch` / `OptionContractMatch` | 60s broker-search cache only | Broker symbol-search endpoints | Deploy/search UI | Displayed, raw detail available |
| `reqMktData` / `tickSnapshot` for option chain | `market_data.stream_option_chain` | Captured for underlying and each option subscription/tick snapshot | `IbkrChainSnapshot` | Streaming/request-scoped; market data writers are used elsewhere for bars, not chain snapshots | `GET /api/broker/option-chain/{symbol}` SSE | Option chain UI; Audit raw evidence panel | Displayed, raw detail available |
| `reqMktData` / `tickSnapshot` for option surface | `surface.stream_option_surface` | Captured for underlying and option line snapshots | `IbkrSurfaceSnapshot`, `IbkrSurfaceExpiry` | Streaming/request-scoped; surface cache lives in volatility module, not broker evidence | `GET /api/broker/option-surface/{symbol}` SSE | Option surface UI; Audit raw evidence panel | Displayed, raw detail available |
| `reqRealTimeBars` / `realTimeBarList`, `realTimeBar` | `bars.stream_realtime_bars`, live bar subscription helpers | Captured at subscription and per bar | `IbkrBarsSnapshot` / bar dicts consumed by live engine and chart endpoints | Live bar artifacts under `artifacts/live_bars`; live run bars via engine artifacts | Broker bars/streaming helpers; live-instance Activity chart bars | Activity price chart / live engine bar feeds; Audit raw evidence panel | Displayed, raw detail available |
| `reqPnL` / `pnl` | `pnl.stream_account_pnl` | Captured at subscribe and tick | `IbkrPnLTick` account-level row | `make_pnl_writer` can persist `pnl_{account_id}.parquet` when caller wires it | `GET /api/broker/pnl/stream` SSE; status/current risk projections where available | Status/Risk P&L summaries; Audit raw evidence panel | Displayed, raw detail available |
| `reqPnLSingle` / `pnlSingle` | `pnl.stream_position_pnl` | Captured per contract subscribe and tick | `IbkrPnLTick` position-level row | `make_pnl_writer` can persist per-account P&L rows | `GET /api/broker/pnl/positions/stream?con_ids=...` SSE | Position P&L consumers; Audit raw evidence panel | Displayed where consumers subscribe; raw detail available |
| `placeOrder` / `openOrder`, `orderStatus` | `orders.place_paper_order` | Captured from request envelope and returned `Trade` snapshot | `IbkrOrderAck`, `IbkrOrderEvent`, Activity fill/order rows | Intent WAL, broker activity WAL, `executions.parquet`, `trades.parquet` through live engine writers | `POST /api/broker/orders`; live-instance Activity projection | Activity fills/orders; row drill-down now shows request/callback refs with broker identity; Audit raw evidence panel | Displayed with row-level evidence |
| `reqAllOpenOrders` / `openOrder` | `orders.list_open_orders` | Captured with trade count and trade objects; per-trade event evidence also produced | `IbkrOpenOrder`, `IbkrOrderEvent`, Activity order rows | Broker activity WAL for projected order states; request-scoped open-order list | `GET /api/broker/orders/open`; live-instance Activity projection | Working/Pending orders and Activity terminal/order rows; row drill-down now identity-matched; Audit raw evidence panel | Displayed with row-level evidence |
| `cancelOrder` / `orderStatus` | `orders.cancel_paper_order` | Captured with cancel request and post-cancel trade snapshot | `IbkrOrderEvent`; Activity terminal state when observed | Broker activity WAL / intent mutation evidence when run-owned | `DELETE /api/broker/orders/{order_id}`; live-instance mutation/status surfaces | Audit/Activity mutation surfaces; Audit raw evidence panel | Displayed when broker activity observes terminal state |
| `reqExecutionsAsync` / `execDetails` | `orders.executions_for_reconnect_recovery` and execution recovery paths | Captured in order evidence helpers and Activity session evidence | `IbkrOrderEvent`; `ExecutionRow`; Activity fill rows | `executions.parquet`, `broker_activity.jsonl`, reconstructed Activity repair projection | Broker activity recovery; `GET /api/live-runs/{run_id}/executions`; Activity projection | Activity fills, Recent Trades execution joins, row-level evidence drawer | Displayed with row-level evidence |
| `trades()` local ib_async state / `openOrder`, `orderStatus`, `execDetails` snapshots | `orders.stream_order_events` | Uses request evidence associated with placement/open-order/execution context, not a new request | `IbkrOrderEvent` | Broker activity WAL / engine artifacts via consumers | `GET /api/broker/orders/stream` SSE | Activity table and order state consumers | Displayed; raw evidence depends on associated request |

## Cross-cutting surfaces

| Layer | Current implementation |
|---|---|
| Raw IBKR evidence API | `/api/broker/ibkr/evidence` backfills the bounded in-process recorder; `/api/broker/ibkr/evidence/stream` streams future events. |
| Raw IBKR evidence UI | Bot Control Audit tab renders `app-ibkr-api-evidence-panel`, including data-plane health, broker health, diagnostics, evidence backfill, and evidence SSE. |
| Activity normalized API | `/api/live-instances/{sid}/activity` returns bars, fill markers, position annotations, orders today, broker activity rows, warnings, and evidence refs from one backend-authored projection. |
| Activity normalized UI | Activity tab renders price chart, orders, and broker activity rows from that one projection. This PR adds expandable evidence drawers for normalized event rows. |
| Row-level evidence identity | `ActivityEvidenceRef` now carries `order_ref`, `order_id`, `perm_id`, `exec_id`, and `symbol`. Matching uses concrete broker identity and does not attach unrelated session evidence to order/fill rows. |
| Durable broker artifacts | Durable artifacts are domain-specific: `broker_activity.jsonl`, `broker_callbacks.jsonl`, `executions.parquet`, `trades.parquet`, account/P&L parquet writers, and live bar artifacts. Raw evidence events are diagnostic and bounded in memory. |

## Remaining full-interface gaps

| Gap | Impact | Suggested next slice |
|---|---|---|
| `IbkrClient.require_app_responsive()` uses `reqCurrentTimeAsync` as a liveness probe, but that probe is not part of `IbkrApiRequestName` and is not recorded in the raw evidence stream. | Liveness decisions can be correct while the raw evidence panel does not show the exact probe that justified them. | Add a diagnostics-only evidence request/callback pair for `reqCurrentTimeAsync` or document it as explicitly excluded from broker API evidence. |
| `IbkrClient._on_ib_error()` consumes `errorEvent` for connectivity/error-state transitions, but `errorEvent` is not represented in `IbkrApiCallbackName`. | Operator can see degraded connection state, but the exact IBKR error callback is not in the same raw evidence timeline as request/callback observations. | Add an `ibkr_error` evidence event type or extend callback evidence to include `errorEvent` with `reqId`, code, message, and contract snapshot. |
| Raw evidence is bounded in-process memory, not durable. | After process restart, the trader-facing Activity projection can survive via domain artifacts, but the complete raw request/callback diagnostic stream is gone. | Decide whether raw evidence should remain diagnostics-only or be appended to a per-instance durable evidence log. |
| Market-data/option-chain responses are visible in their domain UI and raw Audit panel, but not all are folded into Activity rows. | Activity is order/trade centered, so full market-data evidence inspection requires the Audit tab. | Keep as-is if Activity remains execution focused; otherwise add market-data-specific Activity filters. |

## Verification

- `PythonDataService/.venv/bin/pytest PythonDataService/tests/routers/test_live_instances.py -k activity_projection_matches_evidence_to_specific_order` — passed, 1 selected.
- `podman exec my-frontend npm test -- --watch=false --include src/app/components/broker/bot-control/reused/broker-activity-table/broker-activity-table.component.spec.ts --include src/app/components/broker/bot-control/tabs/configuration-tab.component.spec.ts` — passed, 24 tests.
- `podman exec polygon-data-service python -c "from app.services.activity_evidence_matching import activity_evidence_ref_from_event, matching_evidence_refs; print('activity evidence helper import ok')"` — passed.
