# IBKR control-plane decommission — Slice 0 design (feed seam)

**Status:** approved-in-conversation (operator, 2026-08-26). Implementation plan follows.

**Predecessors:**
- `docs/audits/ibkr-control-plane-decommission-inventory-2026-08-26.md` — scoping authority (509 lines, read at commit `19f01eb9`). Point-in-time evidence, not implementation authority — every claim used here was re-verified against current code before this design was written.
- Issue [#1813](https://github.com/tim1016/learn-ai/issues/1813) and its pinned correcting comment.
- `docs/architecture/adrs/0048-episode-age-policies-and-the-admission-marker-substrate.md` §4f — the decision this work supersedes (the admission-marker fencing question dissolves once IBKR, the only concurrent writer, is retired).
- Closed #1811 / #1799 — a prior attempt at fencing durable state in this codebase; six real defects documented there (two-store fencing, silent durable-state discard on a store migration, an unconditional fail-open legacy fallback, an acquire race, opt-in-by-default fencing, reads mutating a store's schema). Read before designing anything that looks like a lease, fence, or store migration — none of that shape appears in this slice, and it should stay that way.

## Goal

Establish a broker-neutral data-feed seam so that later slices can delete the IBKR account/order/session control plane without breaking anything that legitimately depends on IBKR as *only* a market-data source: the Alpaca Broker V2 live chart and gallery, Alpaca Start/Resume admission (via persisted market-data capability), the global Angular health banner, and the retained IBKR options-chain/surface pages.

Acceptance claim:

> After this slice, the retained feed-side code imports no module from the account/order/session bucket (`account`, `account_recovery`, `account_truth`, `account_truth_freshness`, `order_history`, `order_previews`, `orders`, `pnl`, `order_error_stream`, `order_evidence`, `order_projection`, and their supporting services). This is proven by an executable structural test, not just asserted in prose. Every live consumer of the feed — Alpaca Start/Resume, the Broker V2 panel chart, the gallery, the global health banner + reconnect, and the options-chain/surface pages — keeps working unmodified in behavior.

**No deletion happens in this slice.** Every file in the account/order/session bucket is untouched. This is a pure extraction-and-repoint operation.

## Operator decisions this design encodes

Decided in conversation, 2026-08-26 — recorded here so a later slice doesn't re-litigate them:

1. **Options-chain/options-surface pages stay**, migrated to sit on the market-data boundary rather than retired. They are architecturally IBKR-specific (no second options-data broker exists or is asked for), so per CLAUDE.md's anti-overengineering rule, no synthetic protocol is introduced for them — they stay `IbkrClient`-typed. "Migrate" for these means: confirm and pin (via test) that they already satisfy the import-boundary constraint; physical relocation into a renamed feed package is Slice 6 work, not this slice.
2. **Tick persistence (`persistence.py`) retires entirely.** Disabled by default, no consumer. Deletion is scheduled for the slice that actually touches it (Slice 4), not bundled here.
3. **Historical IBKR custody/exposure math folds retire explicitly**, including their `docs/math-sources-of-truth.md` / `docs/architecture/engine-authority-map.md` canonical registrations. This is Slice 2–4 work (the code isn't touched in Slice 0).
4. **External-client risk on `/api/live-runs`, `/bot-events`, `/live-instances`, symbol search, and Diagnose is accepted as low enough to proceed with retirement on schedule** (Slice 3) — no reverse-proxy config or checked-in script references either endpoint family, confirming the audit's bounded local-log finding.
5. **The chart/5-second bar seam uses the cheap fix, not a new translation layer.** `IbkrMinuteBar` is relocated out of the mixed `models.py` into a feed-owned module, keeping its current shape and field set verbatim. No new neutral bar type, no translation function. This was chosen over building a fuller `ChartBar`-analog specifically because the acceptance criterion (no account/order/session import) doesn't require it — `bars.py` and `client.py`, which the aggregator legitimately depends on, are already in the "retain" bucket. Building a translation layer for something not required by the actual constraint is exactly the unnecessary-abstraction shape the operator asked to avoid ("keep this a simple clean up operation; if a surface is too hard to migrate, delete it").
6. **Options-chain/surface routes get no new protocol**, for the same reason as #1 — single real implementation, no swap on the horizon.

## Current-code corrections that shaped this design

Verified directly against the live tree (not the audit's prose) before designing:

- `MarketDataFeed` (`app/marketdata/feed.py:86-137`) truly is minute-bar-only — confirmed, not just claimed. There is no sub-minute method on the Protocol, and the module docstring says raw 5-second data is used only for liveness bookkeeping.
- **The 5-second/chart path never goes through `MarketDataFeed` at all.** `panel_chart_data_source.py` calls `LIVE_BAR_AGGREGATOR.ensure_subscribed_5s(...)`, which drives `stream_raw_5s_bars()` (`app/broker/ibkr/bars.py:811-835`) directly against `IbkrClient` — a fully parallel, undocumented feed path. `IbkrMinuteBar` is the wire type for *both* 1-minute and 5-second bars there (distinguished only by `end_ms - start_ms`), and it is the literal return type of `ChartWindowResult.bars`, the canonical chart-window resolver's public field (`live_chart_window.py:54-69`). This is a bigger finding than the audit's framing ("one design gap: panel requests 5s, feed exposes 1m") — it's not a gap in `MarketDataFeed`, it's a second, entirely separate feed implementation.
- `IbkrMinuteBar` (`app/broker/ibkr/models.py:464-487`) carries `source`, `provenance`, `venue`, `use_rth` fields that `MarketDataBar` doesn't have — needed for the live chart's mixed IBKR/Polygon-overlay rendering. This is why relocating (not translating into `MarketDataBar`) is the right cheap fix: forcing it into `MarketDataBar`'s shape would either lose display fidelity or require extending the strategy-facing minute-bar contract with chart-only fields it has no other reason to carry.
- The options-chain/surface routes (`routers/broker.py:475-785`) already call only `require_connected_client()` (`broker_dependencies.py:16-35` — pure session-liveness check) plus `contracts.py`/`market_data.py`/`surface.py` functions that take `client: IbkrClient` directly. **They already satisfy the import-boundary constraint today.** No functional migration needed in this slice.
- `broker_capability_service.py` storage is plain JSON files (`<live_runs_root>/_broker/session_capabilities/<account_id>/<symbol>/`), not SQLite. Both its callers (`bot_runner.py` admission wiring, the SQLite Clerk runtime's `extended_phase_proven_at_ms`) consume it as an injected bound method (`read_latest_for`) — a clean seam already.
- `account_truth_artifacts_root()` (`account_truth_refresh.py:101-105`) is exactly `Path(settings.live_runs_root).parent`. Its two live callers (`panel_data_source.py:209-215`, `sqlite_roster_status.py:87,141`) only ever consume the `Path` — nothing account-specific leaks through.
- `client.py`'s `_on_ib_error` (`:361-390`) is one handler doing three jobs for every IBKR error code: connection-state transition, order-error buffering (`_buffer_order_error`, gated on `_is_order_rejection_error()`), and event-log emission (`_record_broker_event`, unconditional). The three are separable at this one call site by deleting the order-error branch; they are not separated by file today.
- The frontend (`broker-health.service.ts`, `broker-banner.component.ts`) reads `account_id`, `is_paper`, and `condition.title`/`.summary` (rendered verbatim, no client-side reinterpretation) in addition to pure connection-state fields. `condition`'s prose is currently account-flavored ("proves account-level broker evidence can refresh", "Account positions... cannot refresh") even though it's keyed off connection state — it needs rewriting, not just field removal.

## Scope

### In scope

1. **Split `app/broker/ibkr/models.py`.** Extract the bar/connection-only types — `IbkrMinuteBar`, `IbkrBarsSnapshot`, `BarProvenance`, `BarSessionPhase`, `IbkrConnectionHealth`, `BrokerHealthCondition` (confirm the exact type list against the file during planning; anything else that is genuinely bar/connection-shaped and has no account/order dependency goes with them) — into a new feed-owned module. Account/order/session/safety types remain in the original file, unmodified, pending deletion with the control plane in a later slice.
2. **Repoint imports only** in `live_bar_aggregator.py`, `live_chart_window.py`, `chart_projection_service.py`, `bar_persistence.py`, `broker_v2_panel.py`, `broker_v2_gallery.py`, and their tests, to the new module. No signature or behavior changes in this group — this is a pure import-path change.
3. **Rehome `broker_capability_service.py`** as market-data capability: rename the class/functions to say "market data" rather than "broker" (matching the operator's framing — this is a market-data entitlement, not broker control), and point its storage root at a new generic artifact-root setting instead of deriving from `live_runs_root`. Update the mechanical call sites in `bot_runner.py` and the SQLite Clerk runtime's `extended_phase_proven_at_ms` wiring.
4. **Replace `account_truth_artifacts_root()`** for its two live callers (`panel_data_source.py`, `sqlite_roster_status.py`) with a new generic artifact-root helper that resolves to the same directory. `account_truth_refresh.py` itself is untouched here — it is deleted whole in a later slice, once nothing calls it.
5. **Split the connection-health payload.** Drop `safety_verdict` from `IbkrConnectionHealth`. Rewrite `_broker_health_condition`'s prose (`health.py:149-244`) to describe connection/feed state only — no "account-level broker evidence," "positions," or "reconciliation evidence" language. Keep `account_id`, `is_paper`, and all connection-state fields — the frontend banner genuinely renders them today.
6. **Remove order-error coupling from `client.py`.** Delete `_buffer_order_error`, `order_errors_after`, the `_order_error_events` deque, `_next_order_error_seq`, `_ORDER_ERROR_BUFFER_LIMIT`, and the order-rejection branch inside `_on_ib_error`. **Before deleting**, grep for every caller of `order_errors_after` and `OrderErrorEvent` across the tree — the audit says only the already-retiring `order_error_stream.py` depends on it, but that claim gets re-verified at implementation time, not trusted from the doc.
7. **Add one structural import-boundary test.** A test that walks the import graph (or asserts a fixed disallow-list of module names) for the retained feed-side modules — `app/marketdata/*`, the new bar-types module, the capability service, `bars.py`/`client.py`/`health.py`/`config.py`/`event_codes.py`/`keepalive.py`/`recovery_state_machine.py`/`auto_reconnect_monitor.py`, and the options-data modules — and fails if any of them import from the account/order/session bucket. This is the executable proof of this slice's acceptance criterion, and it becomes a regression guard other slices inherit for free.
8. **Confirm, via that same test (or a note beside it), that the options-chain/surface routes already comply.** No code change to `contracts.py`/`market_data.py`/`surface.py`/`symbol_search.py` in this slice.

### Explicitly out of scope (decided, not guessed away — executed later)

| Item | Decision | Executes in |
|---|---|---|
| Tick persistence (`persistence.py`) | Retire entirely | Slice 4 |
| `symbol_search.py` + orphaned `broker-instrument-card` frontend component | No production caller; retire | Slice 1 (orphan cleanup) — not pulled into Slice 0 even though it needs zero migration, to keep this slice's diff reviewable as one concern |
| Options-route physical relocation into a renamed feed package | Deferred | Slice 6 |
| Host-daemon supervisor stop/uninstall | Operational, not a code change | Slice 3 |
| `docs/math-sources-of-truth.md` / `engine-authority-map.md` updates for custody-fold retirement | Retire explicitly | Slice 2–4, whichever actually deletes `fleet_contamination.py` / the custody folds |
| Deleting any account/order/session module, router, or service | — | Slices 1–4 |

## Testing plan

- **Import-path-only changes** (models split, capability rehome, artifact-root helper): update existing tests' imports; no new assertions needed beyond "still passes," since behavior is unchanged. Covers `tests/broker/v2panel/test_chart_projection.py`, `tests/services/test_live_chart_window.py`, `tests/test_live_bar_aggregator.py`, `tests/services/test_broker_capability_service.py`, `tests/routers/test_broker_capability.py`, `tests/broker/ibkr/test_capability.py`, `tests/services/test_bot_start_admission.py`.
- **Real behavior changes** get real test changes: `tests/broker/ibkr/test_health.py` (dropped `safety_verdict` field, rewritten condition prose — assert the new copy, not the old), `tests/broker/ibkr/test_client.py` (order-error buffer methods no longer exist — remove those test cases, don't just skip them), `tests/broker/v2panel/test_deploy_symbol_scoped_health.py` and `tests/operator/test_broker_activity_health.py` (check whether they assert on the removed `safety_verdict` field).
- **New structural test** for the import-boundary acceptance criterion (§ In scope, item 7).
- **Frontend**: check `broker-health.service.spec.ts` / `broker-banner.component.spec.ts` for any fixture asserting on `safety_verdict` (should be none — it was never a field those files read per the code map, but verify). If the TypeScript health-response interface (wherever `IbkrConnectionHealth` is typed on the Angular side) declares `safety_verdict`, remove it there too, to keep the type honest.
- **Full pytest** (`cd PythonDataService && DATA_PLANE_CONTROL_SECRET="" ./.venv/bin/python -m pytest tests -q`) and both linters (`ruff check PythonDataService/app/ PythonDataService/tests/`, `npx eslint Frontend/src/ --max-warnings 0`) before push, per repo convention. Watch for the known flaky LEAN e2e (`Benchmark and performance series has N misaligned values`) — re-run in isolation before treating it as a real failure.
- **OpenAPI contract**: no route signatures change in this slice (health response shape changes, but that's not itself a new route), so confirm whether `contracts/openapi/python-data-service.openapi.json` needs regeneration for the `IbkrConnectionHealth` schema's dropped field — regenerate if the generator picks up response-model changes, per repo rule ("regenerate in the same PR as any route change").

## Risks / things to verify during implementation, not assume from this doc

- The exact type list to extract from `models.py` (item 1) needs a fresh read of the file at implementation time — this doc names the types found during design, not a promise that it's exhaustive.
- The `order_errors_after` / `OrderErrorEvent` caller grep (item 6) is load-bearing — do not delete before confirming.
- Whether the Angular health-response type declares `safety_verdict` needs an actual grep, not an assumption from the code map above.
- Whether `contracts/openapi/python-data-service.openapi.json` actually changes shape from the health-payload edit needs to be checked by running the regeneration script, not inferred.

## Branching

This work starts from `master` on a fresh branch (e.g. `decommission/ibkr-feed-seam-1813`) — not on top of the currently-checked-out `combined/adr-0048-d1-and-test-decomposition` branch, which carries unrelated, already-committed, unpushed work from a different task.
