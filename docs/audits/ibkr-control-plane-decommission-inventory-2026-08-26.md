# IBKR control-plane decommission inventory

> **STATUS: SUPPORTING POINT-IN-TIME EVIDENCE — NOT IMPLEMENTATION AUTHORITY.**
> This audit describes the repository and the observed local runtime at the
> pinned commit. Accepted ADRs, current code, generated contracts, and executable
> tests remain the authorities during implementation.

**Issue:** [#1813 — Decommission the IBKR control plane](https://github.com/tim1016/learn-ai/issues/1813)

**Commit read:** [`19f01eb9d0d1a7a921d026029ae28015bc290edb`](https://github.com/tim1016/learn-ai/tree/19f01eb9d0d1a7a921d026029ae28015bc290edb)

**Date:** 2026-08-26

**Method:** Static import/caller tracing from FastAPI and Angular composition
roots, route-registration inspection, artifact-layout inspection, and a bounded
observation of the running local stack. No source code, runtime artifact, or
GitHub issue was changed by this investigation.

## Executive answer

The product decision in #1813 is crisp, but its three-file keep-list is not a
safe deletion boundary. `client.py`, `bars.py`, and `models.py` are the only
IBKR modules imported directly by `app/marketdata/`, `app/broker/alpaca/`, and
`bot_runner.py`; their **current transitive module closure is twelve**, and
three additional active data-feed paths sit outside that lexical claim:

1. Alpaca Broker V2 live charts and the gallery use the IBKR bar aggregator.
2. Alpaca Start/Resume and extended-session decisions use persisted IBKR
   market-data capability.
3. The global Angular shell polls and controls IBKR connection health.

There is also a live, navigable IBKR options-data product at
`/broker/options-chain` and `/broker/options-surface`. Those routes are not in
the retired redirect set and depend on `contracts.py`, `market_data.py`, and
`surface.py`. They are data-feed surfaces, not broker actuation. Deleting them
would be a separate product decision, not an automatic consequence of retiring
the IBKR control plane.

The safe sequence is therefore **extract and name the data-feed seam first**,
then delete the account/order/session control plane. The end state can still be
small, but the implementation must first make the desired small boundary true.

## Scope corrections to #1813

| Claim | Verified result | Consequence |
| --- | --- | --- |
| `app/broker/ibkr/*.py` count | 29 files including `__init__.py`; **28 substantive modules**. Keeping three lexical roots leaves 25 substantive candidates, not 26. | Treat `__init__.py` as the package marker, not a control-plane module. |
| Three surviving roots | Direct-import claim is true at `app/marketdata/ibkr_feed.py:39-46`; neither `app/broker/alpaca/` nor `app/services/bot_runner.py` directly imports IBKR. The three roots' present transitive closure is 12 modules. | Refactor before deletion; a three-file checkout does not import. |
| Routers | Eight files directly import IBKR, but `broker_dependencies.py` is a dependency helper rather than an `APIRouter`. Nine legacy or mixed routers are actually registered because `broker_session.py` and `live_instances.py` depend on IBKR transitively. Registration is at `app/main.py:599-609,658-698`. | Use the registered route graph, not direct-import count, for API retirement. |
| Services | **24 production service files contain executable IBKR imports.** A 25th textual hit, `live_log_failures.py`, contains only a comment and logger-name string (`:343-344,440`). | Do not call the textual 25-file census 25 code dependencies. |
| Frontend `/broker/*` | Fifteen compatibility URLs are redirect-only at `Frontend/src/app/app.routes.ts:20-36`. Two other `/broker/*` routes are active options-data pages at `:294-306`. | Keep compatibility redirects; separately decide the options-data product. |
| .NET | No production or test reference to the retired REST paths or account-safety types was found in `Backend/` or `Backend.Tests/`. | No .NET implementation slice is currently required. |

## The actual data-feed dependency envelope

The twelve-module closure of the named roots is:

`api_evidence`, `bars`, `client`, `config`, `contracts`, `event_codes`,
`keepalive`, `models`, `order_error_stream`, `order_evidence`,
`order_projection`, and `recovery_state_machine`.

The hidden dependencies are visible in the roots themselves:

- `client.py:27-50` imports connection configuration, event-code sets,
  keepalive, connection models, order-error buffering, the recovery state
  machine, and `BrokerSessionEventService`.
- `bars.py:45-53` imports API evidence, `client`, underlying qualification from
  `contracts`, and bar models.
- `models.py:27-28` imports the recovery-state type and broker safety verdict.
- `contracts.py:23-29` depends on API evidence, client, and models.
- `order_error_stream.py:8-16` depends on order evidence and order projection.

Some dependencies should not survive merely because the current roots import
them. In particular, order-error buffering and broker-session event emission
must be removed from the feed client. Conversely, some modules omitted by the
three-root census are genuinely feed-owned and should survive in a smaller,
broker-neutral form.

### Recommended feed boundary

| Concern | Current evidence | Disposition |
| --- | --- | --- |
| Connection settings | `config.py:32-116` owns host, port, client ID, line cap, read-only mode, enablement, and auto-connect. `:118-173` mixes in live-run, broker-session, host-daemon, and account-gate settings. | Keep a feed-settings subset; move generic artifact-root settings out; delete control-plane fields with their consumers. |
| Socket liveness | `keepalive.py:1-17,40-85` shortens dead-bridge detection so reconnect can observe it. | Keep as feed transport machinery. |
| Reconnect and resubscribe | `auto_reconnect_monitor.py:267-297` handles hard/soft loss; `:328-363` recovers stale subscriptions; `:520-607` reconnects and runs recovery callbacks. | Keep concept and implementation, but register only feed recovery callbacks. |
| Connection/feed health | `health.py:38-93` composes client and monitor state. The neutral `FeedHealth` adds bar freshness in `marketdata/ibkr_feed.py:242-298` but does not expose manual lifecycle or reconnect-attempt state. | Keep one feed-health contract; rewrite account-evidence/operator-control wording at `health.py:149-244`. |
| Feed models | `models.py:460-502` holds bar shapes and `:838-925` connection health, inside a much larger account/order/options model file. | Split feed bar/connection models from control-plane models; do not preserve the monolith merely by naming it a survivor. |
| Bar qualification/telemetry | `bars.py:45-52` requires `contracts.qualify_underlying` and API evidence. | Rehome the stock qualifier and bounded feed telemetry, or remove telemetry after proving no operational need. |
| Session data capability | `broker_capability_service.py:18-60` persists and reads account/symbol-scoped data capability. `bot_runner.py:286-310` injects it into Start and Resume admission. | Keep/rehome as **market-data capability**, not broker-control capability. |
| Live options data | Active routes call `contracts`, `market_data`, and `surface` at `routers/broker.py:475-785`; Angular loads both pages at `app.routes.ts:294-306`. | Keep/rehome if the live options pages remain. A removal needs an explicit product decision. |

At startup, `main.py:248-325` constructs the client and monitor. Its monitor
currently restores both bars and legacy broker activity (`:309-322`); the
broker-activity callback should go. The Account Truth refresh loop at
`:327-341` should go. Installation of `IbkrMarketDataFeed` at `:343-350` and
the Alpaca-only `BotTaskRegistry` at `:358-372` should remain, with a
broker-neutral artifact-root setting. Shutdown has the same split at
`main.py:417-481`: stop Alpaca bot consumers before the feed, but remove Account
Truth and broker-activity teardown.

## Answers required before slice 1

### 1. `chart_projection_service.py` is active and serves Alpaca

It must be migrated, not deleted.

The direct import is only the visible edge:

- `chart_projection_service.py:29,90-111,180-214` maps `IbkrMinuteBar` into
  Broker V2 chart rows and builds the live chart.
- `panel_chart_data_source.py:58-90` subscribes the process-global
  `LIVE_BAR_AGGREGATOR`, resolves a chart window, then calls the projection.
- `live_chart_window.py:18,54-87,318-367` uses and constructs `IbkrMinuteBar`,
  including Polygon overlays represented as the same vendor type.
- `live_bar_aggregator.py:43-46,473` imports IBKR bars/client/models and owns the
  production singleton.
- `broker_v2_panel.py:569-657` exposes the live chart on the active
  `/api/brokers/{broker}/accounts/{account_id}/bots/{sid}/chart/live` path.
- `broker_v2_gallery.py:61,159` uses the same aggregator for the active gallery.

`MarketDataBar` already exists at `marketdata/feed.py:37-60`, and
`IbkrMarketDataFeed` already translates vendor bars at
`marketdata/ibkr_feed.py:310-325`. The chart path should consume that neutral
shape. One design gap remains: `MarketDataFeed` exposes minute bars, while the
panel can request five-second bars (`panel_chart_data_source.py:72-75`). Add a
neutral five-second/chart stream or a deliberately feed-local chart seam before
removing direct IBKR types.

### 2. The feed keeps reconnect, keepalive, settings, and health

Yes, but not the current mixed files wholesale.

The global shell makes this a live product dependency: `AppComponent` always
mounts `BrokerBannerComponent` and starts `BrokerHealthService` at
`Frontend/src/app/app.component.ts:71-77,90-114`. The service polls
`GET /api/broker/health` every five seconds and drives Connect/Disconnect/
Reconnect at `broker-health.service.ts:9-20,74-121`; the banner explicitly calls
it “IBKR market data” at `broker-banner.component.ts:4-10,52-60,70-75`.

Therefore keep connection lifecycle and feed health, preferably behind a
market-data URL/name. Retain the minimal connected-account identity and
paper/live sentinel that protect feed configuration and scope data-entitlement
capability (`marketdata/ibkr_feed.py:96-98`; `client.py:577-603`), but remove
account-wide truth/order safety, actuation verdicts, order buffers,
broker-session history, and broker-activity concerns from the client/health
payload. The existing `/api/market-data-feed/health` route
(`routers/market_data_feed.py:1-49`) is useful bar-freshness evidence but is not
yet a replacement for manual lifecycle and reconnect state.

### 3. `live_runs.py` and `bot_events.py` do not serve the current Alpaca runner

Both routers are registered under `/api/live-runs` at `main.py:658-674`, so
they are reachable APIs. Their IBKR dependency is only the misplaced
`get_settings().live_runs_root` path (`live_runs.py:29,308,597,1092` and
`bot_events.py:15,67-76`). They read historical host-runner directories and
their desired-state/command/log artifacts.

The current Alpaca runner is different:

- `bot_runner.py:1-24` states it is an in-container task registry with no host
  daemon or subprocess.
- It owns `live_state/<strategy_instance_id>/` (`:7-10`) and is constructed for
  broker `alpaca` only at `main.py:358-372`.
- Broker V2 run history is served by `BotRunEvidenceService` through
  `BotTaskRegistry.run_history` at `bot_runner.py:1316-1330`, not through
  `live_runs.py`.
- No production Angular caller for `/api/live-runs` or `/bot-events` was found.
  The checked-in OpenAPI contract still publishes those paths.

Bounded runtime evidence agrees with the static trace but does not prove absence
of external clients. In the current 3.5-hour local log window there were zero
requests for `live-runs`, `bot-events`, or `live-instances`; there were 1,165
requests for `/api/broker/health` and seven Alpaca chart/live requests. The host
daemon was still listening, but process inspection found no child run; legacy
connection/session logs were still updating. This is enough to classify the two
readers as **not the current Alpaca path**, not enough to claim every file under
`live_runs_root` is dormant.

The sample was taken from `podman logs --timestamps polygon-data-service
--since 48h`; retained output spanned 2026-08-26 15:59:24–19:33:11 CT. Request
paths were grouped with the method/path token only, so the audit does not copy
account or strategy IDs from the log. Filesystem activity was checked at
`PythonDataService/artifacts/live_runs/_broker/connection_events.jsonl`,
`_broker/session_roster_history.jsonl`, and the newest per-run directory. The
first two were current on 2026-08-26, while the newest per-run directory was
last modified 2026-07-29. Process and socket inspection showed the host daemon
started at 09:14:34 CT and listening on TCP 8765. These observations describe
one developer runtime, not production traffic.

Disposition: retire the `live_runs` and `bot_events` API readers after checking
for external clients beyond the observed window. Preserve or archive their
historical artifacts; never delete them as cleanup. Split `ARTIFACTS_ROOT`,
`live_state`, session-capability snapshots, and connection logs away from the
legacy `live_runs_root` name before removing that setting.

### 4. The live options pages require an explicit decision

The options pages are active navigation, not components hidden behind a retired
redirect:

- They are loaded at `Frontend/src/app/app.routes.ts:294-306` and appear in the
  Options menu at `Frontend/src/app/shell/app-menu.ts:52-59`.
- The chain requests expirations/strikes and streams `/api/broker/option-chain`
  (`broker-options-chain.component.ts:225,274,301`).
- The surface requests expirations/strikes and streams
  `/api/broker/option-surface` (`broker-options-surface.component.ts:240,297,366`).
- Backend endpoints are at `routers/broker.py:475-785` and depend on IBKR
  contract qualification, quote streaming, and surface assembly.

If “IBKR is only a data feed” includes live options, migrate these routes under
a market-data boundary. If the product intends to retire them, say so explicitly
and remove their menu/routes/components/contracts as their own reversible slice.

## Complete `app/broker/ibkr` module inventory

| Disposition | Modules | Reason |
| --- | --- | --- |
| Retain, but narrow/rehome | `bars`, `client`, `config`, `event_codes`, `health`, `keepalive`, `models`, `recovery_state_machine`, `auto_reconnect_monitor` | Core transport, bars, recovery, and health. `client`, `config`, `event_codes`, and `models` are mixed and need control-plane code removed. |
| Active dependency; decouple before deciding deletion | `api_evidence`, `contracts` | `bars` currently imports both. Retain only feed qualification/telemetry or fold it into the feed boundary. |
| Data-feed product decision | `capability`, `market_data`, `surface`, `persistence`, `symbol_search` | Capability is active in Alpaca admission. Options market data/surface are active. Persistence is optional tick archival. Symbol search is a read-only feed endpoint, but its Angular instrument-card consumer is currently unreachable from a production route. |
| Retire after hidden client dependency is removed | `order_error_stream`, `order_evidence`, `order_projection` | The surviving client currently buffers order rejection callbacks; that is control-plane residue, not a feed requirement. |
| Retire with account/order control | `account`, `account_recovery`, `account_truth`, `account_truth_freshness`, `order_history`, `order_previews`, `orders`, `pnl` | Account, order, P&L, recovery, and what-if/control evidence. |
| Retire or rehome only if a feed diagnostic is still wanted | `diagnostics` | The old Diagnose endpoint is reachable but has no production Angular caller; its connection checks can be folded into feed health if operationally useful. |

This table enumerates all 28 substantive modules. `__init__.py` remains a
package marker as long as any IBKR feed package remains.

## Service inventory

### Five active services to migrate, not delete

| Service | Active consumer | Required change |
| --- | --- | --- |
| `bar_persistence.py` | `live_bar_aggregator.py:46` | Store a neutral chart/feed bar type. |
| `broker_capability_service.py` | `bot_runner.py:154,294,309`; `bot_trade_strategy.py:50`; `broker_v2_panel/panel_data_source.py:68,800`; SQLite Clerk runtime | Rename/rehome as market-data capability and use a generic artifact root. |
| `broker_v2_panel/chart_projection_service.py` | Active Broker V2 live chart and gallery | Accept neutral bars. |
| `live_bar_aggregator.py` | Broker V2 panel, gallery, reconnect callback | Consume the shared feed rather than `stream_minute_bars`/`IbkrClient` directly. |
| `live_chart_window.py` | Broker V2 panel chart data source | Replace `IbkrMinuteBar` protocol/result/Polygon-overlay construction with neutral chart bars. |

### Four services with a smaller surviving seam

| Service | Surviving seam | Control-plane disposition |
| --- | --- | --- |
| `account_truth_refresh.py` | `account_truth_artifacts_root()` is still imported by active Broker V2 panel/roster code (`panel_data_source.py:55,215`; `sqlite_roster_status.py:32,87,141`). | Extract generic artifact-root resolution, then delete the Account Truth refresh loop and service. |
| `broker_session_events.py` | It is imported by `client.py:50`, not by the feed abstraction. | Remove event-history emission from the client, then retire the service. |
| `data_plane_health.py` | `resolved_code_revision()` is used by the jobs route (`routers/jobs.py:73`); process liveness is broker-neutral. | Move `DataPlaneHealth` models out of IBKR models; keep the generic service, optionally move its route. |
| `fleet_contamination.py` | Pure exposure-fold math is separately registered in `docs/math-sources-of-truth.md:93`. | Remove retired IBKR I/O/runtime wiring, preserve or deliberately retire/rehome the pure fold, and update the math registry in the same PR. |

### Fifteen control-plane services to retire

`account_event_journal.py`, `account_reconciliation.py`,
`account_safety_access.py`, `account_safety_snapshot.py`,
`account_truth_snapshot.py`, `activity_evidence_matching.py`,
`bot_event_rejection_bridge.py`, `broker_activity_publisher.py`,
`broker_activity_reconciler.py`, `broker_activity_reconstruction.py`,
`broker_session_history.py`, `broker_session_mirror.py`,
`broker_session_reconciler.py`, `host_capability.py`, and
`journal_recovery.py`.

`live_log_failures.py` is not a 25th code importer. It is imported only by the
legacy `live_runs` router (`live_runs.py:70-74,861`) and classifies historical
runner logs. Retire it with that API unless a separate forensic reader is
explicitly retained; remove its IBKR logger-name rules either way.

Two documentation registries make service deletion more than a file cleanup:
`docs/math-sources-of-truth.md:92-93` still names broker-activity verdict and
fleet-contamination math as canonical, while
`docs/architecture/engine-authority-map.md:46-48` still preserves historical
IBKR activity/binding/admission evidence. Update those authorities in the same
slice as the affected code; historical ADRs stay historical rather than being
rewritten as if they never existed.

## Router and API inventory

The nine registered legacy or mixed router modules expose 66 operations before
the mixed broker router is split: `broker` 22, `broker_account_truth` 3,
`broker_capability` 2, `account_reconciliation` 17, `broker_session` 7,
`live_runs` 9, `bot_events` 2, `live_instances` 2, and `broker_activity` 2.

| Registered surface | Current role | Disposition |
| --- | --- | --- |
| `routers/broker.py` (`/api/broker`) | Mixed: feed lifecycle/health, active options data, account/positions, P&L, orders, evidence, diagnostics, bar snapshots (`:146-967`). | Split. Keep/rehome feed health/lifecycle and chosen options-data routes; retire account/order/P&L/evidence routes. |
| `routers/broker_capability.py` | Probe/read session market-data capability. | Rehome under market data; it feeds active Alpaca admission. |
| `routers/broker_account_truth.py` | Account Truth, what-if order, completed orders. | Retire. |
| `routers/account_reconciliation.py` | IBKR account roster, safety, cockpit, journal repair, reconciliation, triage, events. | Retire. Do not remove the separately registered Clerk transaction router. |
| `routers/broker_session.py` | Session mirror/history/purge/SSE. | Retire. |
| `routers/broker_activity.py` | Legacy live-instance activity REST/SSE. | Retire with evidence projector. |
| `routers/live_instances.py` | Host daemon health/lease compatibility. | Retire with host bridge after stopping its supervisor/installations. |
| `routers/live_runs.py` | Historical host-run reader/control sidecars. | Retire after external-client check; preserve artifacts. |
| `routers/bot_events.py` | Historical run-authored bot event REST/SSE. | Retire with legacy run reader; preserve artifacts. |
| `routers/market_data_feed.py` | Neutral shared-feed freshness diagnostic. | Keep and extend or compose with lifecycle health. |
| `routers/clerk_transactions.py` | Active Alpaca transaction history plus explicit `broker=ibkr` compatibility (`:82,101-114,166,171-179`). | Keep the Alpaca route; remove only the IBKR branch, Postgres reader, and legacy query vocabulary. |

`routers/broker_dependencies.py` is removable support code after the last legacy
route using it is gone; it should not be counted as a router.

## Secondary Python cleanup outside the 29/8/24 headline counts

Direct-import counts miss transitive schemas and compatibility readers. Each of
these must be classified in the matching retirement slice:

- `app/main.py` startup, callbacks, router registration, and shutdown.
- `app/broker/runtime_snapshot.py`, which projects the IBKR client into legacy
  broker runtime evidence.
- `app/engine/live/account_clerk_journal.py`,
  `account_clerk_journal_models.py`, `broker_callbacks.py`, and
  `journal_exposure.py`, which import IBKR order models for historical custody
  evidence. Preserve raw artifacts and pure registered math; remove executable
  compatibility readers only after their consumers and registries are updated.
- `app/engine/live/host_daemon_client.py` and `app/services/host_capability.py`,
  plus `app/engine/live/host_daemon.py`, `daemon_auth.py`, compatibility lease
  writing, and the two host-daemon installer scripts. The host daemon is still
  running locally, so repository deletion must be paired with an explicit
  supervisor/service shutdown and uninstall handoff.
- `app/schemas/account_truth.py`, `broker_session.py`, and the API-evidence
  dependency in `broker_capability.py`; split the surviving capability schema
  from IBKR control schemas.
- `app/services/account_directory.py`, `account_cockpit.py`,
  `account_journal_authority.py`, `clerk_transaction_projection.py`,
  `clerk_transaction_projection_store.py`,
  `clerk_transaction_projection_ibkr.py`, and `clerk_custody_timeline.py` where
  they support only the retired IBKR account or `broker=ibkr` transaction path.
  The activated SQLite/Alpaca transaction projection remains.
- `compose.yaml:189` still describes the container as staying up for the
  live-runs router. Remove or correct that deployment statement with the route.
- `contracts/data-plane-control-surfaces.json` and
  `contracts/openapi/python-data-service.openapi.json` still publish legacy
  routes. Regenerate the OpenAPI client/contracts mechanically after each API
  slice.

There are 59 Python test files with IBKR module references, 57 of which contain
executable imports. They are not all retirement candidates:
bar/feed/reconnect/capability/options tests should migrate with the surviving
seam, while account/order/session/live-run tests should be removed or replaced
by structural retirement contracts. Classify by retained behavior, not by
import text alone.

## Frontend inventory

The 15 compatibility redirects at `app.routes.ts:20-36` already satisfy the
repository rule and should remain redirect-only. They have no component behind
them at runtime. The following production areas are orphaned from the Angular
composition root and can be deleted with their templates, styles, specs, and
fixtures:

- `components/broker/account-freeze-banner/`
- `components/broker/account-roster/`
- `components/broker/account-safety/`
- `components/broker/account-truth-board/`
- `components/broker/broker-operation-result/`
- `components/broker/broker-orders/`
- `components/broker/broker-session-mirror/`
- `components/broker/shared/operator-blocker-list/`
- `components/broker/account-desk/account-desk-directory-store.service.ts`
- `services/broker-session-mirror.service.ts`
- `services/broker-connectivity.service.ts`
- orphan standalone helpers `components/broker/ibkr-portal.ts` and
  `components/broker/operator-severity.ts`

Do **not** delete these mixed or active areas:

- `components/broker/account-desk/` still contains the Alpaca transaction
  history used by `components/brokers/alpaca-desk/`.
- `components/broker/clerk-transaction-evidence-drawer/` is part of that active
  transaction history.
- `components/broker/broker-deploy-page/` supplies the Alpaca deploy drawer to
  the desk and Broker V2 bot list.
- `components/broker/v2-panel/` is canonical Alpaca bot control.
- `services/brokers.service.ts` is the active Alpaca Broker V2 client.
- `services/broker-sse.ts` is used by both active options pages.
- `services/broker-health.service.ts`, `shell/broker-banner.component.ts`, and
  the health/lifecycle methods of `services/broker.service.ts` are active feed
  controls and need repointing, not deletion.
- Transaction methods in `services/broker.service.ts` are used by the active
  Alpaca account desk. Split this mixed service; do not delete it wholesale.
- `broker-options-chain/` and `broker-options-surface/` remain unless the
  explicit options-data decision retires them.

`shared/broker-instrument-card/` is currently unreachable from a production
route and is the only production caller of `BrokerService.searchSymbols`. It can
retire with the symbol-search endpoint after the external-client check, or be
made part of the retained options-data product deliberately.

## Runtime/artifact safety

The observed filesystem contained 41 historical per-run `live_runs/`
directories and 195 `live_state/` directories. File names under historical
runs include `run_ledger.json`, `broker_callbacks.jsonl`, `bot_events.jsonl`, and
`host_daemon.log`; current Alpaca lifecycle files live under `live_state/`.
These are evidence, not disposable build output.

Required handling:

1. Stop/uninstall the host-daemon supervisor before removing its code or token/
   lease readers.
2. Move retired authority/evidence trees to a documented archive location if
   needed; never recursively delete them as part of a code cleanup.
3. Preserve opaque IDs, hashes, receipt paths, and historical ADRs.
4. Do not delete `_broker/session_capabilities` while Alpaca Start/Resume still
   reads it; migrate it to the feed capability store first.
5. Do not delete or rename the common artifacts root while active Alpaca
   `live_state`, lifecycle receipts, and chart persistence still use it.

## Recommended reversible slices

### Slice 0 — establish the feed seam (must precede deletion)

- Decide whether live options chain/surface remain.
- Add broker-neutral bar/chart types, including the five-second requirement.
- Repoint chart projection, live chart window, live bar aggregator, persistence,
  gallery, and panel to the shared feed seam.
- Rehome feed capability and generic artifact-root settings.
- Split connection/feed health, reconnect, keepalive, and feed event codes from
  account/order/session concerns.
- ~~Remove order-error and broker-session-event coupling from `IbkrClient`.~~
  **Deferred to Slice 4, not removed in Slice 0.** Both couplings turned out to
  have a second live consumer outside Slice 0's scope (`orders.py:689` for
  order-error buffering; `broker_session_mirror.py`/`broker_session_history.py`/
  `routers/broker_session.py` for `broker_session_events`) — removing either
  now would break a still-registered endpoint. Both are named, tracked
  exceptions in the Slice 0 structural test
  (`tests/structural/test_ibkr_feed_boundary.py`), not silently dropped.

Acceptance: the retained feed imports no account/order/session module (modulo
the two tracked, Slice-4-closing exceptions above); Alpaca Start/Resume, panel
chart, gallery, global health banner, reconnect, and chosen options pages work
through the new seam.

### Slice 1 — orphaned account-safety UI and projection

- Remove the orphan Angular account-safety/freeze/roster/truth components and
  their dedicated clients/types.
- Remove account-safety snapshot/access/presented-action routes/services/schemas
  after extracting any still-needed generic helper.
- Preserve account-safety artifacts as forensic evidence.

Acceptance: redirect-only routes remain redirects; no active Alpaca import is
removed; add a structural retirement test.

### Slice 2 — Account Truth, reconciliation, and IBKR transaction compatibility

- Remove Account Truth refresh/startup/shutdown, account directory/cockpit,
  reconciliation/journal recovery, account events, and IBKR account modules.
- Remove only `broker=ibkr` from the otherwise-active Clerk transaction router
  and delete its legacy Postgres/journal projection readers.
- Extract the generic artifacts root before deleting
  `account_truth_artifacts_root()`.
- Remove account routes while preserving the separately registered active
  Alpaca Clerk transaction endpoints.

Acceptance: Alpaca desk transactions and Broker V2 lifecycle evidence remain;
no Account Truth task starts; archived authority files are untouched.

### Slice 3 — broker session, activity, host bridge, and historical run APIs

- Remove session mirror/history/reconciler, broker activity publisher/routes,
  host capability/live-instances routes, and legacy run/bot-event readers.
- Once `broker_session_mirror.py` is gone, remove the `safety_verdict` field
  from `IbkrConnectionHealth` (`app.broker.safety_verdict.BrokerSafetyVerdict`)
  — deferred out of Slice 0 because that mirror was its second live caller.
  Reword the `health.py` "recovering" branch's title/summary too (Slice 0 only
  reworded its `remediation` line, since the title/summary's "account-evidence
  recovery" language was still true while this field existed). Delete the
  corresponding `_ALLOWED_EXCEPTIONS` entry in
  `tests/structural/test_ibkr_feed_boundary.py`.
- Stop/uninstall the running host daemon and remove compatibility lease/config/
  installer wiring.
- Preserve historical run, callback, event, and log evidence.
- Update `engine-authority-map`, math registry rows, ADR provenance notes, and
  OpenAPI/control-surface contracts.

Acceptance: a longer retained log/client inventory finds no external consumer;
the current Alpaca runner/history remains; no host daemon is listening after the
operational cutover.

### Slice 4 — order, P&L, account evidence, and mixed client cleanup

- Remove IBKR account, order, what-if, completed-order, P&L, order history/
  projection/evidence, and persistence paths not selected as feed features.
- Remove account/order endpoints from the mixed broker router.
- Trim models/config/event codes/API evidence to the proven feed envelope.
- Remove `IbkrClient`'s order-error buffering (`order_error_stream.OrderErrorEvent`)
  and its `broker_session_events` emission — both deferred out of Slice 0
  because each had a second live consumer there. Delete the corresponding
  `_ALLOWED_EXCEPTIONS` entries in `tests/structural/test_ibkr_feed_boundary.py`
  once removed; the test should still pass with two fewer exceptions.
- Retire tick persistence (`persistence.py`) entirely — operator decision,
  no archival intent stated.

Acceptance: an import-closure test pins the intended feed module boundary and
proves no IBKR order mutation/account authority remains.

### Slice 5 — frontend/API/client consolidation

- Delete the remaining orphan components/services/types/specs.
- Split `BrokerService` into retained market-data lifecycle/options and active
  Alpaca transaction clients, or rename its retained portions to state the seam.
- Regenerate OpenAPI and generated TypeScript; update menu tests and redirect
  tests without attaching behavior to retired aliases.

Acceptance: Angular production reachability includes no retired IBKR control
component; the global feed banner and active Alpaca desk still work.

### Slice 6 — final feed hardening and receipt

- Collapse temporary compatibility modules so the final IBKR feed package has
  the intentionally documented API, rather than the original twelve-module
  accidental closure.
- Retain tests for connection/read-only sentinel, keepalive, reconnect,
  subscription recovery, bar timestamps/freshness, capability scope, active
  charts, and any retained options data.
- Add a retirement receipt listing every removed symbol, route, config field,
  test family, contract entry, supervisor, and preserved artifact location.

Acceptance: full Python/Angular suites and linters pass; OpenAPI is regenerated;
the final import graph contains only feed concerns; #1813 records the final
keep-list derived from code rather than the original lexical guess.

## Remaining uncertainties

- The bounded local log window cannot rule out an external `/api/live-runs`,
  `/bot-events`, `/live-instances`, symbol-search, or Diagnose client. Check
  longer reverse-proxy retention and any operator scripts before removal.
- Whether live options remain is a product decision. Current code and menu say
  they do; #1813's “three files only” list implies they do not.
- Tick persistence is disabled by default but is a legitimate data-feed concern.
  Decide archival intent before deleting `persistence.py` and its config.
- Historical IBKR custody/exposure folds are still named in documentation
  authorities. Decide whether to preserve them as offline forensic math or
  retire them explicitly; do not silently remove a registered canonical path.
- The host daemon is currently idle, not absent. Operational decommissioning is
  required in addition to repository deletion.
