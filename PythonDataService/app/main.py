"""FastAPI application entry point"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from app.broker.ibkr.client import (
    BrokerError,
    ConnectionRefusedDueToSentinelError,
    IbkrClient,
    set_client,
)
from app.config import settings
from app.routers import (
    account_pnl_attribution,
    account_reconciliation,
    aggregates,
    alpaca_bot_control_examples,
    alpaca_clerk_sqlite,
    baselines,
    bot_events,
    broker,
    broker_account_truth,
    broker_bots,
    broker_capability,
    broker_session,
    broker_v2_gallery,
    broker_v2_panel,
    brokers,
    chart,
    clerk_transactions,
    data_quality,
    dataset,
    edge,
    engine,
    exhaustive_runs,
    golden_fixtures,
    indicator_reliability,
    indicators,
    iv30,
    iv_recorder,
    jobs,
    lean_sidecar,
    market_data_feed,
    market_monitor,
    monte_carlo,
    options,
    portfolio,
    quantlib_options,
    recency,
    research,
    research_divergence,
    research_runs,
    sanitize,
    snapshot,
    spec_strategy,
    strategy,
    strategy_validation,
    tickers,
    volatility,
    walk_forward,
)
from app.routers import (
    broker_activity as broker_activity_router,
)
from app.routers import (
    live_instances as live_instances_router,
)
from app.routers import (
    live_runs as live_runs_router,
)
from app.security.data_plane_control import (
    require_data_plane_control_secret,
    require_data_plane_control_secret_always,
)
from app.services.account_reconciliation import AccountReconciliationService
from app.services.account_truth_refresh import AccountTruthRefreshLoop, account_truth_artifacts_root
from app.services.fleet_contamination import record_account_journal_parity_observation
from app.utils.error_handlers import (
    polygon_exception_handler,
    request_validation_exception_handler,
)

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

def _alpaca_clerk_configuration_is_valid() -> bool:
    """Clear stale runtime state, validate settings, and log only safe detail."""
    from pydantic import ValidationError

    from app.broker.alpaca.clerk.active_authority import set_active_clerk_runtime
    from app.broker.alpaca.config import (
        alpaca_configuration_error_detail,
        get_alpaca_settings,
    )

    set_active_clerk_runtime(None)
    try:
        get_alpaca_settings()
    except ValidationError as exc:
        logger.warning(
            "Alpaca settings invalid; order-submission clerk not installed.",
            extra={"detail": alpaca_configuration_error_detail(exc)},
        )
        return False
    return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events.

    The IBKR client is connected best-effort: a failure here logs and
    leaves the broker endpoints in a 503 state, but the rest of the
    service still boots. The ONLY failure that aborts startup is the
    paper-vs-live sentinel mismatch — that's a safety violation and
    must not be silently absorbed.

    When broker is disabled: /health returns HTTP 200 with disabled=True
    (not 503 — Angular HttpClient routes 503 to error path); /diagnose
    returns DiagnosticReportDisabled; all other broker endpoints return 503.
    """
    logger.info(f"Starting Polygon Data Service on {settings.HOST}:{settings.PORT}")
    logger.info(f"Polygon API Key configured: {bool(settings.POLYGON_API_KEY)}")

    # Broker System v2 — register the phase-1 read brokers (Alpaca only) so
    # /api/brokers/{broker}/... can resolve them. Cheap and keyless: the client
    # builds credentials and network lazily on first call. Independent of the
    # IBKR (v1) lifecycle below.
    from app.broker.alpaca.broker import register_default_brokers
    from app.broker.contract.registry import get_broker_registry

    register_default_brokers()
    logger.info(
        "Broker v2 registry ready: %s", get_broker_registry().registered_brokers()
    )

    # Alpaca Clerk (phase 2) — the in-process single-writer for order
    # submission. Installed only when Alpaca keys are present, independent of
    # the IBKR broker_enabled block below (Alpaca is a peer vendor, not part of
    # the IBKR gateway lifecycle). No keys → no clerk → the write endpoint
    # honestly reports "not configured".
    from app.broker.alpaca.broker import AlpacaBroker
    from app.broker.alpaca.clerk.active_authority import (
        ActiveClerkRuntime,
        select_active_clerk_runtime,
        set_active_clerk_runtime,
    )
    from app.broker.alpaca.config import get_alpaca_settings

    alpaca_clerk_runtime: ActiveClerkRuntime | None = None
    sovereign_equity_snapshot_scheduler = None
    if _alpaca_clerk_configuration_is_valid():
        from app.broker.alpaca.clerk.stream_health import build_default_stream_health_gate

        alpaca_broker = AlpacaBroker()
        alpaca_clerk_root = get_alpaca_settings().clerk_dir
        # #1671: scheduled session structure remains calendar-owned; this
        # source provides the separate real-time clock + per-symbol
        # halt/resume evidence used to fail new exposure closed.
        from app.broker.alpaca.market_liveness import (
            AlpacaMarketLivenessConsumer,
            set_market_liveness_consumer,
        )

        alpaca_market_liveness = AlpacaMarketLivenessConsumer.for_alpaca(
            read=alpaca_broker,
        )
        alpaca_market_liveness.start()
        set_market_liveness_consumer(alpaca_market_liveness)
        logger.info("Alpaca market-liveness source started.")
        # S4 (#1262): the dual-health submission gate — market-data feed AND
        # trade_updates execution channel must both be healthy. Shared by
        # both authorities so the cutover never silently drops this
        # fail-closed check.
        alpaca_stream_health_gate = build_default_stream_health_gate()

        # Resolve the broker account before constructing the sole writer. The
        # append-only activation fence must select SQLite; a missing or invalid
        # activation installs no broker-mutation capability.
        alpaca_clerk_runtime = await select_active_clerk_runtime(
            read=alpaca_broker,
            trade=alpaca_broker,
            artifacts_root=alpaca_clerk_root,
            stream_health_gate=alpaca_stream_health_gate,
        )
        set_active_clerk_runtime(alpaca_clerk_runtime)
        if alpaca_clerk_runtime.clerk is not None:
            logger.info(
                "Alpaca Clerk ready (authority=%s).",
                alpaca_clerk_runtime.authority_kind,
            )

            # Capture/parsing stays shared, but durable lifecycle evidence is
            # folded only into the SQLite authority.
            from app.broker.alpaca.trade_updates import (
                TradeUpdatesConsumer,
                set_trade_updates_consumer,
            )

            alpaca_trade_updates = TradeUpdatesConsumer.for_alpaca(
                evidence_sink=alpaca_clerk_runtime.evidence_sink,
                read=alpaca_broker,
            )
            alpaca_trade_updates.start()
            set_trade_updates_consumer(alpaca_trade_updates)
            logger.info("Alpaca trade_updates consumer started (live lifecycle enabled).")
        elif alpaca_clerk_runtime.startup_failure is not None:
            logger.warning(
                "Alpaca Clerk unavailable after authority selection.",
                extra={
                    "reason_code": alpaca_clerk_runtime.startup_failure.reason_code,
                    "account_id": alpaca_clerk_runtime.startup_failure.account_id,
                },
            )

        from app.services.sovereign_equity_snapshots import (
            DailySovereignEquitySnapshotScheduler,
            DailySovereignEquitySnapshotStore,
            DailySovereignEquitySnapshotWriter,
            sovereign_equity_snapshot_database_path,
        )

        sovereign_equity_snapshot_scheduler = DailySovereignEquitySnapshotScheduler(
            writer=DailySovereignEquitySnapshotWriter(
                store=DailySovereignEquitySnapshotStore(
                    sovereign_equity_snapshot_database_path(alpaca_clerk_root)
                ),
                account_snapshot_provider=alpaca_broker.get_account,
            )
        )
        sovereign_equity_snapshot_scheduler.start()
        logger.info("Daily sovereign Alpaca equity snapshot scheduler started.")

    from app.broker.ibkr.config import get_settings as get_ibkr_settings

    ibkr_settings = get_ibkr_settings()
    ibkr_client: IbkrClient | None = None
    # Auto-reconnect monitor (broker-stability hardening). Started after the
    # initial connect attempt regardless of its outcome — even a startup
    # failure should auto-retry rather than wait for an operator click.
    from app.broker.ibkr.auto_reconnect_monitor import (
        AutoReconnectMonitor,
        set_monitor,
    )

    monitor: AutoReconnectMonitor | None = None
    account_truth_refresh_loop = None

    if ibkr_settings.broker_enabled:
        ibkr_client = IbkrClient()
        # Install the client immediately so /health reports the
        # disconnected-but-available state and POST /api/broker/connect can
        # drive the lifecycle from the Status page. Without this, a soft-fail
        # auto-connect leaves _client=None and the only fix is restarting
        # the container.
        set_client(ibkr_client)
        # The operator-intended state is set BEFORE the connect attempt so
        # the monitor (started further down) knows whether to retry on
        # initial-connect failure: when ``connect_on_startup`` is True the
        # operator wants a live link and a soft-fail should auto-retry;
        # when False the operator wants the client idle until they click
        # Connect — the monitor must NOT auto-connect against that intent.
        ibkr_client.set_desired_connected(ibkr_settings.connect_on_startup)
        if ibkr_settings.connect_on_startup:
            try:
                await ibkr_client.connect()
                logger.info("IBKR client connected; broker endpoints available.")
            except ConnectionRefusedDueToSentinelError:
                # Hard fail — never proceed past a paper/live mismatch.
                logger.exception("IBKR sentinel mismatch — aborting startup.")
                raise
            except (BrokerError, OSError) as exc:
                # Soft fail — Gateway is probably not running locally. Broker
                # endpoints will return 503 until POST /api/broker/connect.
                # The auto-reconnect monitor below picks it up on the next tick.
                logger.warning(
                    "IBKR client could not connect (%s). Auto-reconnect monitor will retry; "
                    "POST /api/broker/connect or the Status page will also drive a manual attempt.",
                    exc,
                )
        else:
            logger.info(
                "IBKR auto-connect disabled (IBKR_CONNECT_ON_STARTUP=false). "
                "Use POST /api/broker/connect or the Status page to establish the connection."
            )
        # The monitor is started even when initial connect failed — it will
        # observe the disconnected state and retry per the backoff policy.
        # Slice 3 / ADR 0011 amendment — the broker-activity publisher
        # registry's reconnect-recovery sweep rides the same chain as
        # the bar aggregator's resubscribe-all. Order inside the wrapped
        # chain: bar aggregator first (restore read-side market-data
        # subscriptions), then the broker-activity sweep (replay the day's
        # executions to catch evidence missed mid-drop).
        #
        from app.services.broker_activity_publisher_registry import (
            get_publisher_registry as get_broker_activity_publisher_registry,
        )
        from app.services.live_bar_aggregator import LIVE_BAR_AGGREGATOR

        async def _sweep_broker_activity_after_reconnect() -> None:
            await get_broker_activity_publisher_registry().sweep_all_for_recovery()

        monitor = AutoReconnectMonitor(
            ibkr_client,
            recovery_callbacks=[
                LIVE_BAR_AGGREGATOR.resubscribe_all,
                _sweep_broker_activity_after_reconnect,
            ],
        )
        monitor.start()
        set_monitor(monitor)

        artifacts_root = account_truth_artifacts_root(ibkr_settings)
        live_runs_root = Path(ibkr_settings.live_runs_root)
        reconciliation_service = AccountReconciliationService(artifacts_root=artifacts_root)

        account_truth_refresh_loop = AccountTruthRefreshLoop(
            client=ibkr_client,
            artifacts_root=artifacts_root,
            account_truth_observer=reconciliation_service.observe_account_truth,
            account_truth_failure_observer=reconciliation_service.observe_account_truth_failure,
            account_journal_observer=lambda account_id: record_account_journal_parity_observation(
                live_runs_root,
                account_id=account_id,
            ),
        )
        account_truth_refresh_loop.start()

        # Shared MarketDataFeed — installed after the IBKR client is created
        # so it references the same process-local client the rest of the broker
        # stack uses. The feed is read-only (no orders); it is the one sanctioned
        # cross-broker surface (phase-3 design §4, #1258 L2).
        from app.marketdata.ibkr_feed import IbkrMarketDataFeed, set_market_data_feed

        set_market_data_feed(IbkrMarketDataFeed(ibkr_client))
        logger.info("Shared MarketDataFeed installed (ibkr, in-process fan-out).")
    else:
        set_client(None)
        set_monitor(None)
        logger.info(
            "IBKR broker disabled (IBKR_BROKER_ENABLED=false). Broker endpoints disabled. Live-runs router available."
        )

    # ── Alpaca Bot Control v2, S2 (#1260) — in-container bot runner ────
    # The task registry is installed regardless of broker_enabled so the
    # /api/brokers/{broker}/bots routes answer honestly: without the shared
    # MarketDataFeed a deploy fails with a typed 503, and the artifact-derived
    # list stays readable. No host daemon anywhere in this path (L1/P10).
    from app.marketdata.ibkr_feed import get_market_data_feed
    from app.services.bot_runner import BotTaskRegistry, set_bot_task_registry

    bot_task_registry = BotTaskRegistry(
        artifacts_root=Path(ibkr_settings.live_runs_root).parent,
        feed_resolver=get_market_data_feed,
        supported_broker_ids=frozenset({"alpaca"}),
    )
    set_bot_task_registry(bot_task_registry)
    logger.info("In-container bot runner installed (task registry, daemon-free).")

    # S5 (#1263) — boot recovery sweep, BEFORE any bot may start (fail
    # closed): the Clerk recovers and reconciles SQLite authority first; runner
    # restoration candidates are then projected into typed interrupted evidence.
    # Starts stay refused while any intent remains uncertain.
    _boot_clerk = (
        alpaca_clerk_runtime.clerk
        if alpaca_clerk_runtime is not None
        else None
    )
    if _boot_clerk is not None:
        async def _unresolved_intents() -> int:
            return await _boot_clerk.unresolved_effect_count()

        await bot_task_registry.run_boot_recovery(
            recover=_boot_clerk.recover,
            reconcile=_boot_clerk.reconcile_once,
            unresolved_intents_probe=_unresolved_intents,
        )
    else:
        await bot_task_registry.run_boot_recovery()

    # Start the Alpaca reconciliation sweep AFTER boot recovery so the periodic
    # sweep cannot race the boot reconciliation pass (both call reconcile_once).
    _pending_sweep = (
        alpaca_clerk_runtime.sweep
        if alpaca_clerk_runtime is not None
        else None
    )
    if _pending_sweep is not None:
        _pending_sweep.start()
        logger.info(
            "Alpaca reconciliation sweep started (authority=%s).",
            alpaca_clerk_runtime.authority_kind,
        )

    # Start the shared fleet snapshot before serving its REST/SSE readers.
    # Per-bot state is owned by the Alpaca Broker V2 projection runtime above.

    try:
        yield
    finally:
        if sovereign_equity_snapshot_scheduler is not None:
            await sovereign_equity_snapshot_scheduler.stop()
        # Stop the in-container bot tasks first — they consume the shared
        # MarketDataFeed, which is torn down later in this block. Operator
        # desired-state is preserved; outcomes record SERVICE_SHUTDOWN.
        await bot_task_registry.stop_all()
        set_bot_task_registry(None)
        # Stop the Alpaca reconciliation sweep + live-lifecycle consumer first —
        # cancel their background tasks (and the consumer's socket) cleanly,
        # independent of the IBKR teardown.
        from app.broker.alpaca.clerk.active_authority import set_active_clerk_runtime
        from app.broker.alpaca.market_liveness import (
            get_market_liveness_consumer,
            set_market_liveness_consumer,
        )
        from app.broker.alpaca.trade_updates import (
            get_trade_updates_consumer,
            set_trade_updates_consumer,
        )

        alpaca_trade_updates = get_trade_updates_consumer()
        if alpaca_trade_updates is not None:
            await alpaca_trade_updates.stop()
            set_trade_updates_consumer(None)
        alpaca_market_liveness = get_market_liveness_consumer()
        if alpaca_market_liveness is not None:
            await alpaca_market_liveness.stop()
            set_market_liveness_consumer(None)
        set_active_clerk_runtime(None)
        if alpaca_clerk_runtime is not None:
            await alpaca_clerk_runtime.close()
        from app.broker.alpaca.clerk.sqlite.process_repositories import (
            close_all_repositories,
        )

        close_all_repositories()
        from app.services.broker_v2_panel.live_projection import stop_live_projection_hubs

        await stop_live_projection_hubs()
        await bot_events.get_bot_event_stream_service().stop_all()
        if account_truth_refresh_loop is not None:
            await account_truth_refresh_loop.stop()
        # ADR 0014 — stop every broker-activity publisher before tearing
        # down the broker connection so each publisher's WAL append +
        # subscriber drain completes cleanly. Safe to call even when no
        # publishers were registered (registry stop_all is a no-op).
        from app.services.broker_activity_publisher_registry import (
            get_publisher_registry,
        )

        await get_publisher_registry().stop_all()
        # Stop the broker monitor BEFORE disconnecting so a tick-in-flight
        # doesn't observe the close and immediately try to reconnect.
        if monitor is not None:
            await monitor.stop()
            set_monitor(None)
        if ibkr_client is not None and ibkr_client.is_connected():
            await ibkr_client.disconnect()
        set_client(None)
        # Clear the shared MarketDataFeed after the IBKR client is down.
        from app.marketdata.ibkr_feed import set_market_data_feed as _clear_feed

        _clear_feed(None)
        logger.info("Shutting down Polygon Data Service")


app = FastAPI(
    title="Polygon Data Service",
    description="Data fetching and sanitization service for Polygon.io market data",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=settings.get_trusted_hosts(),
)


# CORS middleware for C# backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.get_allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
DATA_PLANE_CONTROL_DEPENDENCIES = [Depends(require_data_plane_control_secret)]
PROTECTED_DATA_PLANE_READ_DEPENDENCIES = [Depends(require_data_plane_control_secret_always)]

app.include_router(aggregates.router, prefix="/api/aggregates", tags=["aggregates"])
app.include_router(sanitize.router, prefix="/api", tags=["sanitize"])
app.include_router(indicators.router, prefix="/api/indicators", tags=["indicators"])
app.include_router(options.router, prefix="/api/options", tags=["options"])
app.include_router(snapshot.router, prefix="/api/snapshot", tags=["snapshot"])
app.include_router(market_monitor.router, prefix="/api/market", tags=["market"])
app.include_router(tickers.router, prefix="/api/tickers", tags=["tickers"])
app.include_router(strategy.router, prefix="/api/strategy", tags=["strategy"])
app.include_router(spec_strategy.router, prefix="/api/spec-strategy", tags=["spec-strategy"])
app.include_router(research.router, prefix="/api/research", tags=["research"])
app.include_router(recency.router, prefix="/api/research/recency", tags=["research-recency"])
app.include_router(indicator_reliability.router, prefix="/api/research", tags=["research"])
# Research-pipeline walk-forward (Phase C). Registered BEFORE
# ``research_runs`` so the literal ``/walk-forward`` segment wins
# against the ``GET /{run_id}`` route on the parent router.
app.include_router(
    walk_forward.router,
    prefix="/api/research/strategy-runs/walk-forward",
    tags=["research-walk-forward"],
)
app.include_router(
    exhaustive_runs.router,
    prefix="/api/research/exhaustive-runs",
    tags=["research-exhaustive-runs"],
)
# Research-pipeline Monte Carlo (Phase D). Same pre-research_runs
# placement so the literal ``/monte-carlo`` segment wins.
app.include_router(
    monte_carlo.router,
    prefix="/api/research/strategy-runs/monte-carlo",
    tags=["research-monte-carlo"],
)
# Research-pipeline null baselines (Phase E1). Same pre-research_runs
# placement so the literal ``/baselines`` segment wins.
app.include_router(
    baselines.router,
    prefix="/api/research/strategy-runs/baselines",
    tags=["research-baselines"],
)
# Research-pipeline run ledger (Phase A of build-alpha-style features 1-8).
app.include_router(research_runs.router, prefix="/api/research/strategy-runs", tags=["research-runs"])
# Trading-calendar preview — sibling endpoint under ``/api/research`` so
# the date-picker UI can surface skipped sessions before a run is
# submitted. Lives in a separate ``APIRouter`` instance from the
# strategy-runs router because their prefixes differ.
app.include_router(
    research_runs.calendar_router,
    prefix="/api/research",
    tags=["research-trading-calendar"],
)
app.include_router(dataset.router, prefix="/api/dataset", tags=["dataset"])
app.include_router(data_quality.router, prefix="/api/data-quality", tags=["data-quality"])
app.include_router(volatility.router, prefix="/api/volatility", tags=["volatility"])
app.include_router(engine.router, prefix="/api/engine", tags=["engine"])
# LEAN Sidecar Lab — data-plane API in front of the launcher service.
# Phase 2a exposes only the trusted sample; Phase 3+ unlocks user
# algorithm source. See docs/architecture/lean-sidecar-lab.md.
app.include_router(lean_sidecar.router, prefix="/api/lean-sidecar", tags=["lean-sidecar"])
app.include_router(chart.router, prefix="/api/chart", tags=["chart"])
# Portfolio scenario / live-Greeks. Phase 2 of numerical-authority migration:
# Python becomes canonical for portfolio Greeks; .NET becomes a passthrough.
app.include_router(portfolio.router, prefix="/api/portfolio", tags=["portfolio"])
# QuantLib option pricing endpoints (/status, /price, /strategy, /compare).
# Registration was dropped by 88b48ac (IV-surface refactor) on 2026-04-12;
# the four endpoints silently 404'd until pricing-lab surfaced it.
app.include_router(quantlib_options.router, prefix="/api/quantlib", tags=["quantlib"])
# Internal job orchestration (Redis-backed). Mounted under /api/jobs-internal;
# the public surface is the .NET /api/jobs facade in Backend/Jobs/JobsApi.cs.
app.include_router(jobs.router, prefix="/api/jobs-internal", tags=["jobs-internal"])
# Edge router carries its own /api/edge prefix.
app.include_router(edge.router)
# Live IV30 endpoints (vix-style + parametric) — Step C of IV-ownership plan.
# Router carries its own /api/edge/iv30 prefix.
app.include_router(iv30.router)
# IV recorder (POST /api/iv-recorder/snapshot, GET .../series/{ticker}) —
# Step D of IV-ownership plan. Driven by .NET cron; not in-process.
app.include_router(iv_recorder.router)
# /research/data-divergence/* — dashboard + matrix endpoints. The router
# carries its own prefix so we mount it bare.
app.include_router(research_divergence.router)
# Shared MarketDataFeed diagnostic surface — read-only feed health + fan-out
# subscription count. Requires the always-on control secret (GET exposes live
# broker state: connection status, last bar watermark, subscription count).
app.include_router(
    market_data_feed.router,
    prefix="/api/market-data-feed",
    dependencies=PROTECTED_DATA_PLANE_READ_DEPENDENCIES,
)
# Interactive Brokers paper-trading endpoints (Phase 1: read-only chain).
# Router carries its own /api/broker prefix.
app.include_router(broker.router, dependencies=DATA_PLANE_CONTROL_DEPENDENCIES)
# IBKR account/session capability probe (issue #1005 Slice 0).
app.include_router(broker_capability.router, dependencies=DATA_PLANE_CONTROL_DEPENDENCIES)
# Account Truth and account-wide broker ledger endpoints.
app.include_router(broker_account_truth.router, dependencies=DATA_PLANE_CONTROL_DEPENDENCIES)
# Account-scoped reconciliation and recovery triage endpoints.
app.include_router(account_reconciliation.router, dependencies=DATA_PLANE_CONTROL_DEPENDENCIES)
# Broker session mirror — read-only roster/SSE observatory with sensitive runtime data.
app.include_router(broker_session.router, dependencies=PROTECTED_DATA_PLANE_READ_DEPENDENCIES)
# Broker System v2 read surface (/api/brokers/{broker}/...). Broker account,
# position, order, activity, asset, and clock evidence is sensitive operator
# data, so every v2 read requires the always-on data-plane control secret.
app.include_router(
    brokers.router,
    dependencies=PROTECTED_DATA_PLANE_READ_DEPENDENCIES,
)
# Broker-parameterized bot runner (Alpaca Bot Control v2, S2 — #1260).
# Deploy/stop/list for in-container log-only bots; broker-tagged bindings.
# Control actions on live broker state — always-on data-plane secret.
app.include_router(
    broker_bots.router,
    dependencies=PROTECTED_DATA_PLANE_READ_DEPENDENCIES,
)
# Broker-v2 bot control panel contracts + projections (S1 — #1297).
# panel-profile / catalog / panel / presented-actions / chart (live + bounded
# history). Account-scoped reads and control actions on live broker state, so
# every route requires the always-on data-plane control secret.
app.include_router(
    broker_v2_panel.router,
    dependencies=PROTECTED_DATA_PLANE_READ_DEPENDENCIES,
)
# Aggregated bot gallery wall (S4 — snapshot + SSE stream across every
# running bot's tiles and shared per-symbol bars). Same sensitivity as
# broker_v2_panel: always-on data-plane control secret required.
app.include_router(
    broker_v2_gallery.router,
    dependencies=PROTECTED_DATA_PLANE_READ_DEPENDENCIES,
)
# Static fixture-envelope contract for the unlinked Clerk diagnostic gallery.
# The Angular example imports these committed documents locally and never calls
# this read-only OpenAPI anchor.
app.include_router(alpaca_bot_control_examples.router)
# Golden fixture catalog — reads manifest.json + artifacts/fixture-validation/latest.json.
# No live computation at request time (see docs/process/autonomous-decisions.md D-010).
app.include_router(golden_fixtures.router, prefix="/api", tags=["golden-fixtures"])
app.include_router(
    strategy_validation.router,
    prefix="/api/strategy-validation",
    tags=["strategy-validation"],
    dependencies=DATA_PLANE_CONTROL_DEPENDENCIES,
)
# Authored bot-event stream backfill (ADR 0024 / PRD #928). This is run-scoped
# historical evidence; live delivery comes in a later SSE slice.
app.include_router(
    bot_events.router,
    prefix="/api/live-runs",
    tags=["bot-events"],
    dependencies=DATA_PLANE_CONTROL_DEPENDENCIES,
)
# Live paper-trading run observer (read-only). Three-layer caching:
# Layer 1: 15 s TTL on dir listing; Layer 2: mtime-signature LRU on status;
# Layer 3: inode-tracked incremental deque on log tail.
app.include_router(
    live_runs_router.router,
    prefix="/api/live-runs",
    tags=["live-runs"],
    dependencies=DATA_PLANE_CONTROL_DEPENDENCIES,
)
app.include_router(
    live_instances_router.router,
    prefix="/api/live-instances",
    tags=["live-instances"],
    dependencies=PROTECTED_DATA_PLANE_READ_DEPENDENCIES,
)
app.include_router(clerk_transactions.router, dependencies=PROTECTED_DATA_PLANE_READ_DEPENDENCIES)
app.include_router(account_pnl_attribution.router, dependencies=PROTECTED_DATA_PLANE_READ_DEPENDENCIES)
# Activation-selected SQLite Alpaca Clerk command and projection surface.
# The active-authority selector fails closed instead of falling back to JSONL.
# PROTECTED_DATA_PLANE_READ_DEPENDENCIES (not the mutating-only DEPENDENCIES
# above): every route on this router is either a mutation or a sensitive read
# (positions, holds, operation/order identities, recovery tokens, DB
# identity, timeline proof refs) — the GET routes need the secret checked
# unconditionally, like clerk_transactions.router's comparable reads.
app.include_router(alpaca_clerk_sqlite.router, dependencies=PROTECTED_DATA_PLANE_READ_DEPENDENCIES)
# ADR 0014 — broker-activity reconciliation surface (SSE + REST backfill).
# The router carries its own ``/api/live-instances`` prefix internally
# (so the path is sibling to the live-instances router), keeping the
# operator-facing URL space consistent.
app.include_router(
    broker_activity_router.router,
    dependencies=PROTECTED_DATA_PLANE_READ_DEPENDENCIES,
)

# Data lake (Slice 1a) — gated by DATA_LAKE_ENABLED.
# When disabled, the prefix has no registered routes; clients get 404.
if settings.DATA_LAKE_ENABLED:
    from app.routers import data_lake as data_lake_router

    app.include_router(data_lake_router.router)
    logger.info("data lake routes ENABLED")
else:
    logger.info("data lake routes disabled (set DATA_LAKE_ENABLED=true to enable)")

# Dev-only broker fault-injection seam (PRD #1354) — gated by
# ALPACA_FAULT_INJECTION_ENABLED. When disabled the prefix has no registered
# routes (clients get 404); the seam ALSO refuses to arm off a paper posture.
# Registered behind the always-on data-plane control secret like every broker
# control route. Never enable in a live/production path.
if settings.ALPACA_FAULT_INJECTION_ENABLED:
    from app.routers import alpaca_fault_injection as alpaca_fault_injection_router

    app.include_router(
        alpaca_fault_injection_router.router,
        dependencies=PROTECTED_DATA_PLANE_READ_DEPENDENCIES,
    )
    logger.warning(
        "ALPACA FAULT INJECTION seam ENABLED (dev only, paper-only). "
        "Never enable this in a live/production path."
    )

# Exception handlers. Register request validation separately so rejected
# non-finite JSON numbers cannot make FastAPI's own 422 body non-serializable.
app.add_exception_handler(
    RequestValidationError,
    request_validation_exception_handler,
)
app.add_exception_handler(Exception, polygon_exception_handler)


@app.get("/health")
async def health_check():
    """Health check endpoint for Docker"""
    return {
        "status": "healthy",
        "service": "polygon-data-service",
        # git HEAD baked in at build (GIT_COMMIT_SHA); None if unset. Lets the
        # operator console confirm the data plane matches master and flag drift
        # against the host daemon's live git_sha.
        "git_sha": settings.GIT_COMMIT_SHA or None,
    }


@app.get("/")
async def root():
    """Root endpoint with API info"""
    return {"service": "Polygon Data Service", "version": "1.0.0", "docs": "/docs", "health": "/health"}
