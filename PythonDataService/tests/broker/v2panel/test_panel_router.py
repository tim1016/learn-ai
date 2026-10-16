"""HTTP seam coverage for the active SQLite Broker V2 panel."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport

from app.broker.alpaca.clerk.account_authority import synthetic_account_id_for_strategy
from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    get_active_clerk_runtime,
    set_active_clerk_runtime,
)
from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run
from app.broker.alpaca.clerk.sqlite.decision_receipts import SqliteDecisionReceipts
from app.broker.alpaca.clerk.sqlite.exit import accept_exit
from app.broker.alpaca.clerk.sqlite.reconcile import AccountReconciliationResult
from app.broker.alpaca.clerk.sqlite.reconciliation_sweep import ReconciliationSweep
from app.broker.alpaca.clerk.sqlite.recovery_policy import SWEEP_LIVENESS_BOUND_MS
from app.broker.alpaca.clerk.sqlite.repository import (
    ClerkSqliteRepository,
    ExecutionLeaseLost,
    RepositoryPoisoned,
)
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.sqlite.scheduled_end import ScheduledEnd
from app.broker.alpaca.clerk.sqlite.uncertainty import raise_account_hold, raise_uncertainty
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import EXIT_STUCK_REASON_CODE, ExitStuckCause
from app.broker.contract.errors import BrokerOrderRejected
from app.broker.contract.models import BrokerAccountSnapshot, BrokerOrderLeg
from app.broker.contract.registry import (
    get_broker_registry,
    reset_broker_registry_for_testing,
)
from app.broker.fleet.agent_identity import _FrameInjector
from app.broker.fleet.internal_http import DEFAULT_MAX_EVENT_BYTES, SseEvent, iter_sse_events
from app.broker.ibkr.bar_models import IbkrMinuteBar
from app.engine.live.identity import INSTANCE_ID_PATTERN
from app.routers import alpaca_clerk_sqlite, broker_v2_gallery, broker_v2_panel
from app.routers.broker_v2_panel import router
from app.schemas.alpaca_clerk_sqlite import RecoveryActionCheckRequest, RecoveryActionExecuteRequest
from app.schemas.bot_end import BotEnd, BotEndView, RecordedEnd
from app.schemas.broker_bots import BotStatusView
from app.schemas.broker_v2_panel import (
    BotPanelLiveSnapshot,
    BotPanelView,
    ChartLiveResponse,
    LiveSnapshotUnavailableDetail,
    RunFillView,
    SimulatedFillView,
)
from app.schemas.decision_explanation import DecisionBarRecord, DecisionExplanationRecord, ExplainedCheckRecord
from app.schemas.live_runs import BotDutyOutcomeView
from app.schemas.run_admission import RunAdmissionDecision
from app.services import broker_account_snapshot, surface_hub
from app.services.bot_end import bot_end_view
from app.services.bot_runner import get_bot_task_registry, set_bot_task_registry
from app.services.bot_runner_errors import InvalidStrategyInstanceIdError, UnknownBotError
from app.services.broker_v2_panel import (
    live_projection,
    panel_chart_data_source,
    panel_data_source,
)
from app.services.broker_v2_panel.action_execution_service import (
    reset_idempotency_store_for_testing,
)
from app.services.broker_v2_panel.bot_page_projection import RUN_FILL_LIMIT
from app.services.broker_v2_panel.chart_projection_service import (
    build_live_chart,
    chart_feed_view,
    live_window,
    position_pane,
)
from app.services.live_chart_window import CHART_FEED_NOT_EXPECTED, ChartWindowResult
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS
from tests.broker.alpaca.clerk.sqlite.budgeted_run_fixture import (
    accept_budgeted_test_enter,
    submit_budgeted_test_enter,
    submit_budgeted_test_run,
)
from tests.broker.alpaca.clerk.sqlite.conftest import (
    FIXTURE_RTH_MS,
    _broker_position_fixture,
    _FakeTradePort,
    _make_held_position,
    _TestClock,
    _walk_clock_to,
)
from tests.broker.alpaca.clerk.sqlite.historical_run_fixture import seed_historical_run_started
from tests.broker.alpaca.clerk.sqlite.test_envelope_reservations import _append_slice
from tests.broker.alpaca.clerk.sqlite.test_scheduled_end import (
    _Schedule,
    schedule,  # noqa: F401 -- the runner's schedule of the bots' ends, installed for one test
)
from tests.broker.v2panel.conftest import account_snapshot
from tests.broker.v2panel.fixtures import ACCT, SID

_T0 = 1_700_000_000_000


def _run_id(sid: str) -> str:
    return f"run-{sid}"


def _fleet_sids(size: int) -> tuple[str, ...]:
    """``SID`` first (so single-bot tests are the ``size == 1`` case),
    then ``size - 1`` siblings. Kept short: a strategy instance id over 25
    characters is rejected downstream by order-identity namespacing."""
    return (SID, *(f"bot-{index:02d}" for index in range(1, size)))


class _FakeBrokerPort:
    broker_id = "alpaca"

    def __init__(self) -> None:
        # #1776 WP2: reads are pure, so this counter is the acceptance gate.
        self.calls = 0
        self.methods: list[str] = []

    async def get_account(self) -> BrokerAccountSnapshot:
        self.calls += 1
        self.methods.append('get_account')
        return account_snapshot()

    async def list_positions(self) -> list:
        self.calls += 1
        self.methods.append('list_positions')
        return []

    async def list_orders(self, **_kwargs) -> list:
        self.calls += 1
        self.methods.append('list_orders')
        return []

    async def list_activities(self, **_kwargs) -> list:
        self.calls += 1
        self.methods.append('list_activities')
        return []

    async def submit(self, *_args: object, **_kwargs: object) -> None:  # pragma: no cover - not used
        raise AssertionError("panel read tests must not submit broker orders")

    async def cancel(self, _order_id: str) -> None:  # pragma: no cover - not used
        raise AssertionError("panel read tests must not cancel broker orders")

    async def get_order_by_client_order_id(self, _client_order_id: str) -> None:
        return None

    def capabilities(self) -> None:  # pragma: no cover - registry shape only
        raise NotImplementedError


class _FakeRegistry:
    def __init__(
        self,
        artifacts_root: Path | None = None,
        *,
        running: bool = True,
        sids: Sequence[str] = (SID,),
    ) -> None:
        self.artifacts_root = artifacts_root or Path("receipts")
        self._running = running
        self._sids = tuple(sids)
        # How the bot's run ended, when a test says; a stopped run is otherwise a clean Stop.
        self.duty_outcome: BotDutyOutcomeView | None = None
        # The owner-set end the bot's desired state records, when a test says (#2607).
        self.end: RecordedEnd | None = None
        # Attributed per bot, not counted as a scalar: a fleet-wide total is
        # met by the gallery's fan-out alone, so a per-bot panel read that
        # stopped projecting custody would still clear a total-only bound.
        self.custody_projections: list[tuple[str, str]] = []

    async def preview_resume_admission(self, broker: str, sid: str) -> RunAdmissionDecision:
        """Resolve custody through the real production projection.

        Faking this away would make the call-budget gate vacuous: the
        stopped-bot preview is exactly where the per-poll reconciliation
        used to happen. Only the admission *policy* is out of scope here,
        so the decision itself is reported as unavailable.
        """
        from app.services.bot_runner import BotRunnerError
        from app.services.bot_start_admission import default_start_custody_projection

        async with default_start_custody_projection(
            self.binding_for_control(broker, sid)
        ) as (snapshot, _policy, _terms):
            self.custody_projections.append((sid, snapshot.reconciliation_state))
        raise BotRunnerError("resume admission policy is not modelled in this harness")

    def status(self, broker: str, sid: str) -> BotStatusView:
        assert broker == "alpaca"
        if re.fullmatch(INSTANCE_ID_PATTERN, sid) is None:
            # The real registry confines the id before it looks for a binding,
            # and answers an id it could never bind with its own 422 type.
            raise InvalidStrategyInstanceIdError(
                f"Bot id {sid!r} is not a valid strategy instance id.",
                detail="A bot's id is one path segment of letters, digits, '_', '.' or '-'.",
            )
        if sid not in self._sids:
            raise UnknownBotError(
                f"No bot '{sid}' is bound to broker '{broker}'.",
                detail="Deploy the bot first; bindings are broker-tagged.",
            )
        return BotStatusView(
            strategy_instance_id=sid,
            strategy_key="deployment_validation",
            strategy_label="Deployment Validation",
            broker="alpaca",
            symbol="SPY",
            mode="trade",
            quantity=1,
            running=self._running,
            phase="ON_DUTY",
            desired_state="RUNNING",
            active_run_id=_run_id(sid),
            duty_outcome=self.duty_outcome,
            binding_created_at_ms=_T0,
            last_transition_at_ms=_T0,
        )

    def binding_for_control(self, broker: str, sid: str) -> SimpleNamespace:
        self.status(broker, sid)
        return SimpleNamespace(
            strategy_instance_id=sid,
            run_id=_run_id(sid),
            symbol="SPY",
            use_rth=True,
            strategy_key="deployment_validation",
            strategy_params=None,
            sealed_program=None,
            exit_terms=None,
            mode="trade",
        )

    def dry_run_activity(self, broker: str, sid: str) -> list:
        self.status(broker, sid)
        return []

    def dry_run_restoration_state(self, sid: str) -> None:
        """No boot restoration is in flight in this harness (#2582)."""
        return None

    def foreign_binding(self, sid: str) -> None:
        """Every bot in this harness is sealed on the installed account (#2694)."""
        return None

    def unbound_dry_run(self, sid: str) -> None:
        """No Dry Run is left without its binding here; an id that could name none is refused as the real
        registry refuses it, by looking for the activation under the simulator's account name."""
        synthetic_account_id_for_strategy(sid)
        return None

    def bot_end(self, broker: str, sid: str) -> BotEndView:
        """No end unless a test says: the owner-set end (#2607) is exercised in ``test_bot_end_routes``."""
        self.status(broker, sid)
        return bot_end_view(self.end, now_ms=_T0, dry_run=False, running=self._running, use_rth=True)

    def bindings_for_broker(self, broker: str) -> list:
        """No durable dry-run bindings — the catalog is the plain SQLite roster."""
        return []

    def current_run(self, broker: str, sid: str) -> SimpleNamespace:
        """The run each bot started at ``_T0``; a stopped one ended a minute later, as its duty outcome says."""
        self.status(broker, sid)
        kind = "STOPPED" if self.duty_outcome is None else self.duty_outcome.kind
        terminal = None if self._running else SimpleNamespace(recorded_at_ms=_T0 + 60_000, kind=kind)
        return SimpleNamespace(run_id=_run_id(sid), started_at_ms=_T0, terminal_outcome=terminal)

    async def cancel_end(self, sid: str, *, lifecycle_run_id: str, updated_by: str) -> None:
        """The panel's Stop cancels the bot's end before its STOP commits (#2607); this fleet has no end."""
        self.status("alpaca", sid)

    async def stop_after_durable_clerk_stop(
        self, broker: str, sid: str, *, lifecycle_run_id: str, updated_by: str, reason: str
    ) -> None:
        """The in-process quiescence step `stop_bot_decisions` drives after its
        durable SQLite STOP commits (recovery_execution._quiesce_bot_process).
        Recorded, not asserted on, by every test in this module except the
        lease-revival ones, which use it to prove the retried write actually
        reached this step."""
        assert broker == "alpaca"
        self._running = False


@pytest.fixture()
def fleet_size(request: pytest.FixtureRequest) -> int:
    """How many bots ``api`` registers. Indirect-parametrizable; defaults to
    the single-bot fleet every non-budget test in this module expects."""
    return int(getattr(request, "param", 1))


@pytest.fixture()
def api(tmp_path: Path, fleet_size: int):
    reset_broker_registry_for_testing()
    reset_idempotency_store_for_testing()
    set_active_clerk_runtime(None)
    sids = _fleet_sids(fleet_size)
    set_bot_task_registry(_FakeRegistry(tmp_path, sids=sids))  # type: ignore[arg-type]
    port = _FakeBrokerPort()
    get_broker_registry().register(port)  # type: ignore[arg-type]
    repo = ClerkSqliteRepository.initialize(account_id=ACCT, artifacts_root=tmp_path)
    for sid in sids:
        repo.register_strategy_instance(
            strategy_instance_id=sid,
            symbol="SPY",
            config_hash="config-1",
            strategy_key="deployment_validation",
            display_name="Deployment Validation",
            # Production always writes the full binding dump here; the roster now
            # reads the bot's declared mode/quantity/carryover from it rather than
            # assuming `trade`.
            config_json=json.dumps({"mode": "trade", "quantity": 1, "carryover_policy": "FORBID"}),
        )
        submit_budgeted_test_run(
            repo,
            account_id=ACCT,
            strategy_instance_id=sid,
            lifecycle_run_id=_run_id(sid),
        )
    facade = SqliteAlpacaClerkFacade(
        account_mode="paper",
        repo=repo,
        read=port,  # type: ignore[arg-type]
        trade=port,  # type: ignore[arg-type]
    )
    set_active_clerk_runtime(ActiveClerkRuntime(authority_kind="sqlite", clerk=facade))
    app = FastAPI()
    app.include_router(router)
    try:
        yield app, repo
    finally:
        set_active_clerk_runtime(None)
        set_bot_task_registry(None)
        repo.close()
        reset_broker_registry_for_testing()
        reset_idempotency_store_for_testing()


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _clock_seq(start: int = _T0):
    """A controllable millisecond clock, mirroring
    test_corrective_foundation.py's ``_clock_seq`` so the same
    freeze-then-thaw and foreign-writer patterns work here."""
    counter = {"t": start}

    def clock() -> int:
        counter["t"] += 1
        return counter["t"]

    clock.advance = lambda delta: counter.__setitem__("t", counter["t"] + delta)
    return clock


@pytest.fixture()
def lease_lost_api(tmp_path: Path):
    """A single-bot panel wired to a repository with a controllable clock and
    a short execution-lease TTL (1 s), so a test can freeze time past the
    lease and exercise the real ADR 0050 write-path revival end to end --
    not a monkeypatched exception like the tests around
    ``test_lost_execution_lease_is_an_authored_blocker_not_a_raw_500``, which
    prove only the translation, not that the write path actually revives (or
    correctly refuses to) per ADR 0050.

    A real ``ReconciliationSweep`` is wired into the runtime (not started --
    the write path calls its ``revive_now()`` directly, never the background
    heartbeat loop) so the panel action's revival runs through the one
    supervised entry point production uses, with an ``on_lease_revived`` hook
    the test can observe fired exactly like ``BotTaskRegistry.run_lease_recovery``
    would in production (B1: proving the hook fires, not just that the retry
    succeeds, is the whole point of routing through the sweep instead of the
    bare repository CAS).
    """
    reset_broker_registry_for_testing()
    reset_idempotency_store_for_testing()
    set_active_clerk_runtime(None)
    set_bot_task_registry(_FakeRegistry(tmp_path, sids=(SID,)))  # type: ignore[arg-type]
    port = _FakeBrokerPort()
    get_broker_registry().register(port)  # type: ignore[arg-type]
    clock = _clock_seq(start=1_792_072_800_000)
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCT, artifacts_root=tmp_path, clock=clock, lease_ttl_ms=1_000
    )
    repo.register_strategy_instance(
        strategy_instance_id=SID,
        symbol="SPY",
        config_hash="config-1",
        strategy_key="deployment_validation",
        display_name="Deployment Validation",
        config_json=json.dumps({"mode": "trade", "quantity": 1, "carryover_policy": "FORBID"}),
    )
    submit_budgeted_test_run(
        repo, account_id=ACCT, strategy_instance_id=SID, lifecycle_run_id=_run_id(SID)
    )
    facade = SqliteAlpacaClerkFacade(
        account_mode="paper",
        repo=repo,
        read=port,  # type: ignore[arg-type]
        trade=port,  # type: ignore[arg-type]
    )
    hook_calls: list[int] = []

    async def _on_lease_revived() -> None:
        hook_calls.append(repo.clock())

    sweep = ReconciliationSweep(
        repo=repo,
        read=port,  # type: ignore[arg-type]
        trade=port,  # type: ignore[arg-type]
        intake=facade.intake,
        on_lease_revived=_on_lease_revived,
        pricing=UNPRICEABLE_RECOVERY,
    )
    set_active_clerk_runtime(
        ActiveClerkRuntime(authority_kind="sqlite", clerk=facade, sweep=sweep)
    )
    app = FastAPI()
    app.include_router(router)
    try:
        yield app, repo, clock, hook_calls
    finally:
        set_active_clerk_runtime(None)
        set_bot_task_registry(None)
        repo.close()
        reset_broker_registry_for_testing()
        reset_idempotency_store_for_testing()


async def _post_stop_bot_decisions(
    app: FastAPI, *, idempotency_key: str
) -> httpx.Response:
    async with _client(app) as client:
        panel = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel")
        action = next(
            item
            for item in panel.json()["actions"]
            if item["action_id"] == "stop_bot_decisions"
        )
        assert action["enabled"], action
        return await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/actions",
            json={
                "action_id": "stop_bot_decisions",
                "concurrency_token": action["concurrency_token"],
                "idempotency_key": idempotency_key,
            },
        )


@pytest.fixture()
def stopped_api(api, no_golden_validations, tmp_path: Path, fleet_size: int, monkeypatch: pytest.MonkeyPatch):
    """The worst case: a whole fleet that is not running.

    Every panel GET of a stopped bot previously ran a resume-admission
    preview whose custody guard performed a full account reconciliation, so
    an all-stopped fleet is the shape where a per-read reconciliation costs
    the most.

    The gallery wall is a genuine fourth read surface, so its router is
    mounted here too. Its hub cache is module-global and process-lived, so
    it is reset per test, and its 800 ms IO cache is switched off: a gate
    that passes because a request never reached the projection would be
    worthless. Zero TTL makes the gate strictly harder, never easier.

    The account-identity TTL goes the other way and is pinned wide open.
    These gates are about whether read paths reconcile, not about how long
    one cached identity lookup survives; leaving the 60 s production TTL in
    place would make a slow runner that spends more than a minute on the
    read rounds re-fire `get_account` and fail the call-budget gate as if
    a read had regressed to reconciling.
    """
    monkeypatch.setattr(broker_v2_gallery, "_HUB_CACHE", {})
    monkeypatch.setattr(broker_v2_gallery, "_GALLERY_IO_CACHE_TTL_MS", 0)
    monkeypatch.setattr(
        broker_account_snapshot, "_ACCOUNT_SNAPSHOT_TTL_MS", 86_400_000
    )
    app, repo = api
    app.include_router(broker_v2_gallery.router)
    sids = _fleet_sids(fleet_size)
    # The panel reads `running` from the SQLite projection, not the registry,
    # so the runs have to actually stop for this to be the stopped-bot path.
    for sid in sids:
        submit_stop_run(
            repo,
            account_id=ACCT,
            strategy_instance_id=sid,
            lifecycle_run_id=_run_id(sid),
        )
    registry = _FakeRegistry(tmp_path, running=False, sids=sids)
    set_bot_task_registry(registry)  # type: ignore[arg-type]
    return app, repo, registry


_ACCOUNT_ROOT = f"/api/brokers/alpaca/accounts/{ACCT}"
# The four operator-facing read surfaces #1776 WP2 gates: the bots list, the
# gallery wall, the deploy console, and the per-bot panel. `symbol` is the
# deploy view's only parameter and scopes channel health to the fleet's
# symbol — the shape the console actually requests.
_FLEET_READ_PATHS = (
    f"{_ACCOUNT_ROOT}/bots/catalog",
    f"{_ACCOUNT_ROOT}/gallery/snapshot",
    f"{_ACCOUNT_ROOT}/bots/deploy?symbol=SPY",
)


def _read_paths(fleet_size: int) -> tuple[str, ...]:
    """Every gated read for a fleet: the three account-wide surfaces plus one
    panel GET per bot — an operator with the wall open polls all of them."""
    return _FLEET_READ_PATHS + tuple(
        f"{_ACCOUNT_ROOT}/bots/{sid}/panel" for sid in _fleet_sids(fleet_size)
    )


# The regression class these gates catch is per-read, so width across surfaces
# and bots buys far more than depth in rounds. Three rounds is enough to
# separate first-read setup from steady state — the only thing repetition
# discriminates once every read is attributed per bot — and keeps the module
# inside a few seconds of CI time instead of half a minute.
_READ_ROUNDS = 3

# Measured at fleet 50 (2026-08-26): one round of `_read_paths` runs the
# stopped-bot custody projection exactly twice per bot — once inside the
# gallery's fan-out across the whole wall, once inside that bot's own panel
# GET. The catalog and deploy reads project nothing.
_PROJECTIONS_PER_BOT_PER_ROUND = 2


def _assert_every_bot_ran_the_custody_projection(
    registry: _FakeRegistry, fleet_size: int, rounds: int
) -> None:
    """No read may revive the removed Resume admission workflow."""
    assert registry.custody_projections == []


async def _assert_every_read_surface_projected_the_whole_fleet(
    client: httpx.AsyncClient, fleet_size: int
) -> None:
    """Non-vacuity for the account-wide surfaces at the fleet's real size.

    A 200 alone does not prove a read did the work the gate is budgeting:
    a surface that silently reported an empty fleet would spend no calls
    and pass. Each of the three account-wide reads is therefore asserted to
    carry all ``fleet_size`` bots (or, for deploy, the account it was asked
    about) before the repeated-read rounds begin.
    """
    sids = set(_fleet_sids(fleet_size))

    catalog = await client.get(f"{_ACCOUNT_ROOT}/bots/catalog")
    assert catalog.status_code == 200, catalog.text
    assert {row["strategy_instance_id"] for row in catalog.json()} == sids

    gallery = await client.get(f"{_ACCOUNT_ROOT}/gallery/snapshot")
    assert gallery.status_code == 200, gallery.text
    assert {bot["sid"] for bot in gallery.json()["bots"]} == sids

    deploy = await client.get(f"{_ACCOUNT_ROOT}/bots/deploy?symbol=SPY")
    assert deploy.status_code == 200, deploy.text
    assert deploy.json()["account_id"] == ACCT
    # Not merely "some readiness checks were published" — that passes with the
    # account gate red. `broker.account_posture` reads mode, status and both
    # blocked flags off the account snapshot, which is exactly what the
    # duck-typed account stub used to lack, so asserting it ready is what
    # keeps this surface's fixture honest.
    posture = next(
        check
        for check in deploy.json()["readiness_checks"]
        if check["gate_id"] == "broker.account_posture"
    )
    assert posture["ready"] is True, posture["explanation"]


# The deploy projection consults PostgreSQL-backed validation scope. The daily
# workflow supplies its disposable migrated database.
@pytest.mark.slow
@pytest.mark.parametrize("fleet_size", [1, 50], indirect=True)
async def test_reads_of_a_stopped_bot_never_invoke_the_broker_port(
    stopped_api, fleet_size: int
) -> None:
    """Call-budget gate (#1776 WP2): reads are pure.

    The 15 s reconciliation sweep is the sole automatic reconciler. A read
    that reconciles costs 2-4 broker REST calls plus ledger appends under
    the per-account lock panel actions share -- which is what produced
    48-145 s reads and a hot loop at 77% CPU with zero bots running.

    Run over both the single bot and the 50-bot all-stopped fleet: a read
    path that reconciles once per bot is invisible at a fleet of one.
    """
    app, _repo, registry = stopped_api
    port = get_broker_registry().resolve("alpaca")
    paths = _read_paths(fleet_size)

    async with _client(app) as client:
        # One `get_account` resolves the account identity and is cached for the
        # process. That is a cached identity lookup, not a reconciliation, and
        # it is the only broker contact any read is permitted.
        await _assert_every_read_surface_projected_the_whole_fleet(client, fleet_size)
        for path in paths:
            assert (await client.get(path)).status_code == 200, path
        assert port.methods == ["get_account"], port.methods
        port.calls, port.methods = 0, []
        # Cleared with the port counters so the non-vacuity bound below is
        # measured against the rounds alone, with no slack from the warm-up.
        registry.custody_projections.clear()

        for _ in range(_READ_ROUNDS):
            for path in paths:
                assert (await client.get(path)).status_code == 200, path

    # Non-vacuity: the stopped-bot custody path really ran, for every bot —
    # otherwise a gate of "zero broker calls" would pass on a read that never
    # executed.
    _assert_every_bot_ran_the_custody_projection(registry, fleet_size, _READ_ROUNDS)
    assert port.calls == 0, f"a read reached the broker port: {port.methods}"


@pytest.mark.slow
@pytest.mark.parametrize("fleet_size", [1, 50], indirect=True)
async def test_repeated_reads_never_advance_the_control_revision(
    stopped_api, fleet_size: int
) -> None:
    """Revision-drift gate (#1776 WP2): no read appends to the custody ledger.

    `control_revision` advances only through `writes.advance_control_revision`,
    which every ledger append goes through, so a zero delta across the rounds
    is the assertion that no read wrote. That, and only that, is what this
    gate proves.

    It is deliberately *not* an assertion about action staleness. Panel
    actions no longer fence on this revision: #1772/S16 moved the fence to the
    action-scoped `concurrency_token` (see `sqlite_panel_source.py`) precisely
    because the displayed revision is bumped by ordinary panel reads,
    including the action executor's own re-derivation, which made strict
    revision equality unsatisfiable.
    """
    app, repo, registry = stopped_api
    before = repo.control_meta_snapshot().control_revision
    paths = _read_paths(fleet_size)

    async with _client(app) as client:
        await _assert_every_read_surface_projected_the_whole_fleet(client, fleet_size)
        registry.custody_projections.clear()
        for _ in range(_READ_ROUNDS):
            for path in paths:
                assert (await client.get(path)).status_code == 200, path

    _assert_every_bot_ran_the_custody_projection(registry, fleet_size, _READ_ROUNDS)
    assert repo.control_meta_snapshot().control_revision - before == 0


async def test_panel_profile_endpoint(api) -> None:
    app, _repo = api
    async with _client(app) as client:
        response = await client.get("/api/brokers/alpaca/panel-profile")

    assert response.status_code == 200, response.text
    assert response.json()["broker"] == "alpaca"


async def test_catalog_scoped_returns_sqlite_roster(api) -> None:
    app, _repo = api
    async with _client(app) as client:
        response = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/catalog")

    assert response.status_code == 200
    rows = response.json()
    assert [row["strategy_instance_id"] for row in rows] == [SID]
    assert rows[0]["strategy_key"] == "deployment_validation"
    assert rows[0]["desired_state"] == "RUNNING"


async def test_catalog_places_each_bot_on_home_from_its_own_custody_over_http(tmp_path: Path) -> None:
    """Review A5/A2/B6 (#2562): the Home fields through the real route and a
    real repository -- running, holding, finished and Dry Run rows, each
    stopped row dated by its own run's stop instant (never a status
    transition that is empty for a stopped bot), and a Dry Run under a real
    account worded as a Dry Run."""
    reset_broker_registry_for_testing()
    reset_idempotency_store_for_testing()
    set_active_clerk_runtime(None)
    set_bot_task_registry(_FakeRegistry(tmp_path, sids=()))  # type: ignore[arg-type]
    port = _FakeBrokerPort()
    get_broker_registry().register(port)  # type: ignore[arg-type]
    clock = _clock_seq(start=1_792_072_800_000)
    repo = ClerkSqliteRepository.initialize(account_id=ACCT, artifacts_root=tmp_path, clock=clock)
    bots = (("running", "trade"), ("holding", "trade"), ("finished", "trade"), ("simulated", "dry_run"))
    for sid, mode in bots:
        repo.register_strategy_instance(
            strategy_instance_id=sid, symbol="SPY", config_hash="config-1",
            strategy_key="deployment_validation", display_name="Deployment Validation",
            config_json=json.dumps({"mode": mode, "quantity": 1, "carryover_policy": "FORBID"}),
        )
        submit_budgeted_test_run(repo, account_id=ACCT, strategy_instance_id=sid, lifecycle_run_id=_run_id(sid), clock=repo.clock)
    bought = accept_budgeted_test_enter(
        repo, account_id=ACCT, strategy_instance_id="holding", decision_id="dec-holding",
        lifecycle_run_id=_run_id("holding"), leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=2),
    )
    _append_slice(repo, bought, execution_id="holding-buy", quantity=2, source_event_at_ms=_T0, fee=0)
    stopped_at: dict[str, int | None] = {}
    for sid in ("holding", "finished"):
        submit_stop_run(repo, account_id=ACCT, strategy_instance_id=sid, lifecycle_run_id=_run_id(sid), clock=repo.clock)
        stopped_at[sid] = repo.latest_run(sid).stopped_at_ms
    facade = SqliteAlpacaClerkFacade(account_mode="paper", repo=repo, read=port, trade=port)  # type: ignore[arg-type]
    set_active_clerk_runtime(ActiveClerkRuntime(authority_kind="sqlite", clerk=facade))
    app = FastAPI()
    app.include_router(router)
    try:
        async with _client(app) as client:
            response = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/catalog")
    finally:
        set_active_clerk_runtime(None)
        set_bot_task_registry(None)
        repo.close()
        reset_broker_registry_for_testing()
        reset_idempotency_store_for_testing()

    assert response.status_code == 200, response.text
    rows = {row["strategy_instance_id"]: row for row in response.json()}
    assert {sid: (row["group"], row["world_label"]) for sid, row in rows.items()} == {
        "running": ("running", "PAPER · practice money"),
        "holding": ("holding", "PAPER · practice money"),
        "finished": ("finished", "PAPER · practice money"),
        "simulated": ("dry_run", "DRY RUN · simulated cash"),
    }
    assert None not in stopped_at.values()
    assert {sid: rows[sid]["ended_at_ms"] for sid in ("running", "holding", "finished")} == {
        "running": None, "holding": stopped_at["holding"], "finished": stopped_at["finished"],
    }
    assert rows["holding"]["status_explanation"] == "Stopped · still holds 2 SPY · no bot is managing it"
    # A budgeted Deploy includes complete fee evidence, so a bot with no
    # trades has a known zero result.
    assert (rows["finished"]["final_result_usd"], rows["finished"]["trade_count"]) == ("0.00", 0)


async def test_panel_scoped_uses_sqlite_projection(api) -> None:
    app, _repo = api
    async with _client(app) as client:
        response = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel"
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["health"]["desired_state"] == "RUNNING"
    assert len(body["rail"]["stations"]) == 6
    assert "revision" in body
    assert {action["action_id"] for action in body["actions"]}.isdisjoint(
        {"clear_hold", "record_inventory_baseline"}
    )
    # "deployment_validation" is a registered Signal Program (issue #1730
    # Slice 5); this fixture's bot has no v2 seal at all (no sealed_program,
    # no build receipt for this instance's bytes), so the honest build
    # verdict is UNPROVEN, not a fabricated NOT_APPLICABLE -- see
    # prove_running_program_build's docstring in
    # app/services/signal_program_admission.py.
    assert body["sealed_program"] is None
    assert body["program_build"]["state"] == "UNPROVEN"
    assert body["program_build"]["program_key"] == "deployment_validation"


def _accept_enter_then_exit(repo: ClerkSqliteRepository) -> tuple[str, str]:
    """Two real, durable effect operations for SID with no broker contact."""
    entered = accept_budgeted_test_enter(
        repo,
        account_id=ACCT,
        strategy_instance_id=SID,
        decision_id="dec-old",
        lifecycle_run_id=_run_id(SID),
        leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
    )
    exited = accept_exit(
        repo,
        account_id=ACCT,
        strategy_instance_id=SID,
        decision_id="dec-new",
        lifecycle_run_id=_run_id(SID),
        entry_order_ref=entered.order_ref,
    )
    assert exited.effect_operation_id is not None
    return entered.effect_operation_id, exited.effect_operation_id


async def test_panel_selects_the_requested_older_transaction_not_the_newest(api) -> None:
    """#1729 AC #7: selecting an older transaction must render that
    transaction, not the newest unrelated one, through the real HTTP panel
    endpoint."""
    app, repo = api
    old_ref, newest_ref = _accept_enter_then_exit(repo)
    assert old_ref != newest_ref

    async with _client(app) as client:
        response = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel",
            params={"transaction_ref": old_ref},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rail"]["transaction_ref"] == old_ref
    assert body["rail"]["transaction_ref"] != newest_ref


async def test_panel_unresolvable_transaction_ref_is_explicit_absence_not_the_newest_operation(
    api,
) -> None:
    """#1729 AC #6/#7: a ``transaction_ref`` that names no durable transaction
    must render explicit, named absence — never silently substitute the
    bot's newest unrelated operation for it."""
    app, repo = api
    _old_ref, newest_ref = _accept_enter_then_exit(repo)

    async with _client(app) as client:
        response = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel",
            params={"transaction_ref": "effect:never-existed"},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rail"]["transaction_ref"] == "effect:never-existed"
    assert body["rail"]["transaction_ref"] != newest_ref
    assert all(station["state"] == "not_applicable" for station in body["rail"]["stations"])
    assert all(
        "effect:never-existed" in station["receipt"] for station in body["rail"]["stations"]
    )


async def test_live_snapshot_bootstrap_and_sse_share_one_versioned_document(
    api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _repo = api
    async with _client(app) as client:
        panel_response = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel"
        )
    panel = panel_response.json()
    snapshot = BotPanelLiveSnapshot(
        stream_epoch="epoch-new",
        surface_version=7,
        panel=panel,
        live_chart=ChartLiveResponse(
            strategy_instance_id=SID,
            symbol="SPY",
            trading_date_open_ms=_T0,
            trading_date_close_ms=_T0 + 60_000,
            resolution="5s",
            bars=[],
            fill_markers=[],
            overlay_notices=[],
            feed=chart_feed_view(CHART_FEED_NOT_EXPECTED),
            as_of_ms=_T0,
        ),
    )

    class _Hub:
        strategy_instance_id = SID
        stream_epoch = "epoch-new"
        surface_version = 7
        is_available = True

        async def snapshot(self) -> BotPanelLiveSnapshot:
            return snapshot

        def stall(self) -> None:
            return None

        def seconds_until_stall(self) -> None:
            return None

        def subscribe(self) -> asyncio.Queue[BotPanelLiveSnapshot | None]:
            queue: asyncio.Queue[BotPanelLiveSnapshot | None] = asyncio.Queue(maxsize=2)
            queue.put_nowait(snapshot)
            queue.put_nowait(None)
            return queue

        def unsubscribe(self, _queue: asyncio.Queue[BotPanelLiveSnapshot | None]) -> None:
            return None

    async def get_hub(*_args: object, **_kwargs: object) -> _Hub:
        return _Hub()

    monkeypatch.setattr(broker_v2_panel, "get_or_start_live_projection_hub", get_hub)
    monkeypatch.setattr(
        broker_v2_panel, "retain_live_projection_hub", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        broker_v2_panel, "release_live_projection_hub", lambda *_args, **_kwargs: None
    )
    async with _client(app) as client:
        bootstrap = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/live-snapshot",
            params={"resolution": "5s"},
        )
        stream = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/live-stream",
            params={"resolution": "5s", "cursor": "epoch-old:99"},
        )

    assert bootstrap.status_code == 200
    assert bootstrap.json()["surface_version"] == 7
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert "event: reset" in stream.text
    assert "id: epoch-new:7" in stream.text


async def test_stalled_live_projection_producer_is_served_stale_never_live(
    api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2353: a hung producer must not keep serving its last snapshot as live.

    Real: the SQLite panel assembly, the live-projection registry, SurfaceHub,
    the post-action prompt refresh and the router's REST + SSE handlers.
    Faked: the IBKR chart bars, the server clock, and the stall itself (one
    assembly awaits an event, as a hung SQLite/executor read would).
    """
    live_projection.reset_live_projection_hubs_for_testing()
    clock = {"now_ms": _T0, "monotonic": 1_000.0}
    monkeypatch.setattr(surface_hub, "now_ms_utc", lambda: clock["now_ms"])
    monkeypatch.setattr(surface_hub, "monotonic", lambda: clock["monotonic"])

    async def fake_chart(sid, symbol, fills, *, resolution, now_ms):  # type: ignore[no-untyped-def]
        return ChartLiveResponse(
            strategy_instance_id=sid,
            symbol=symbol,
            trading_date_open_ms=now_ms - 60_000,
            trading_date_close_ms=now_ms + 60_000,
            resolution=resolution,
            bars=[],
            fill_markers=[],
            overlay_notices=[],
            feed=chart_feed_view(CHART_FEED_NOT_EXPECTED),
            as_of_ms=now_ms,
        )

    monkeypatch.setattr(panel_chart_data_source, "_build_live_chart_from_fills", fake_chart)
    real_parts = panel_data_source.get_live_snapshot_parts
    hang = {"on": False}
    producer_hung = asyncio.Event()
    release = asyncio.Event()

    async def parts(*args, **kwargs):  # type: ignore[no-untyped-def]
        if hang["on"]:
            producer_hung.set()
            await release.wait()
        return await real_parts(*args, **kwargs)

    monkeypatch.setattr(panel_data_source, "get_live_snapshot_parts", parts)
    stream = None
    try:
        boot = await broker_v2_panel.get_live_snapshot_scoped("alpaca", ACCT, SID, "5s")
        cursor = f"{boot.stream_epoch}:{boot.surface_version}"

        # The producer hangs inside one assembly (the post-action prompt).
        hang["on"] = True
        live_projection.schedule_live_projection_refresh("alpaca", ACCT, SID)
        await asyncio.wait_for(producer_hung.wait(), timeout=2.0)
        clock["now_ms"] += 60_000
        clock["monotonic"] += 60.0

        with pytest.raises(HTTPException) as refused:
            await broker_v2_panel.get_live_snapshot_scoped("alpaca", ACCT, SID, "5s")
        assert refused.value.status_code == 503
        rest_stall = LiveSnapshotUnavailableDetail.model_validate(refused.value.detail)
        assert rest_stall.reason == "PRODUCER_STALLED"
        assert rest_stall.last_produced_at_ms == _T0
        assert rest_stall.observed_at_ms == _T0 + 60_000

        # A (re)connecting stream is told the projection is stale; it is not
        # re-primed with the frozen snapshot.
        response = await broker_v2_panel.stream_live_snapshot_scoped(
            "alpaca", ACCT, SID, "5s", cursor
        )
        stream = response.body_iterator
        first = await asyncio.wait_for(stream.__anext__(), timeout=2.0)
        assert first.startswith("event: stale\n"), first
        stream_stall = LiveSnapshotUnavailableDetail.model_validate_json(
            first.split("data: ", 1)[1]
        )
        assert stream_stall == rest_stall

        # A cold client (no cursor: a page opened or reloaded mid-stall) gets
        # the frozen frame as an explicitly stale bootstrap, then the stall,
        # so the panel and its Pause/Stop/Flatten controls still mount.
        cold_response = await broker_v2_panel.stream_live_snapshot_scoped(
            "alpaca", ACCT, SID, "5s", None
        )
        cold = cold_response.body_iterator
        try:
            bootstrap = await asyncio.wait_for(cold.__anext__(), timeout=2.0)
            assert bootstrap.startswith(f"id: {cursor}\nevent: snapshot\n"), bootstrap
            cold_stale = await asyncio.wait_for(cold.__anext__(), timeout=2.0)
            assert cold_stale.startswith("event: stale\n"), cold_stale
        finally:
            await cold.aclose()

        # Recovery republishes even when nothing semantic changed.
        hang["on"] = False
        release.set()
        recovered = await asyncio.wait_for(stream.__anext__(), timeout=2.0)
        assert "event: snapshot" in recovered, recovered
        fresh = await broker_v2_panel.get_live_snapshot_scoped("alpaca", ACCT, SID, "5s")
        assert fresh.stream_epoch == boot.stream_epoch
    finally:
        release.set()
        if stream is not None:
            await stream.aclose()
        await live_projection.stop_live_projection_hubs()


async def test_live_stream_announces_a_stall_whose_deadline_passed_mid_yield(
    api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stream that resumes just after the stall deadline must not sleep a
    full keepalive before saying so: the stale frame follows at once."""
    app, _repo = api
    async with _client(app) as client:
        panel = (
            await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel")
        ).json()
    clock = {"now_ms": _T0, "monotonic": 1_000.0}
    monkeypatch.setattr(surface_hub, "now_ms_utc", lambda: clock["now_ms"])
    monkeypatch.setattr(surface_hub, "monotonic", lambda: clock["monotonic"])

    async def assemble() -> BotPanelLiveSnapshot:
        return BotPanelLiveSnapshot(
            stream_epoch="assembled",
            surface_version=0,
            panel=panel,
            live_chart=ChartLiveResponse(
                strategy_instance_id=SID,
                symbol="SPY",
                trading_date_open_ms=_T0,
                trading_date_close_ms=_T0 + 60_000,
                resolution="5s",
                bars=[],
                fill_markers=[],
                overlay_notices=[],
                feed=chart_feed_view(CHART_FEED_NOT_EXPECTED),
                as_of_ms=_T0,
            ),
        )

    hub: surface_hub.SurfaceHub[BotPanelLiveSnapshot] = surface_hub.SurfaceHub(
        strategy_instance_id=SID,
        assemble=assemble,
        refresh_interval_seconds=5.0,
    )
    await hub.refresh()

    async def get_hub(*_args: object, **_kwargs: object) -> surface_hub.SurfaceHub:
        return hub

    monkeypatch.setattr(broker_v2_panel, "get_or_start_live_projection_hub", get_hub)
    monkeypatch.setattr(
        broker_v2_panel, "retain_live_projection_hub", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        broker_v2_panel, "release_live_projection_hub", lambda *_args, **_kwargs: None
    )
    response = await broker_v2_panel.stream_live_snapshot_scoped(
        "alpaca", ACCT, SID, "5s", None
    )
    stream = response.body_iterator
    try:
        first = await asyncio.wait_for(stream.__anext__(), timeout=2.0)
        assert "event: snapshot" in first, first

        # The generator resumes after the deadline already passed.
        clock["now_ms"] += 20_500
        clock["monotonic"] += 20.5
        stale = await asyncio.wait_for(stream.__anext__(), timeout=2.0)
        assert stale.startswith("event: stale\n"), stale
    finally:
        await stream.aclose()
        await hub.stop()


# ── #3217: no live frame leaves the lane over the coordinator's per-event cap ──

_FIVE_SECOND_SESSION_DAY_MS = 1_790_170_200_000 + 1  # Wednesday 2026-09-23, a regular NYSE session


def _five_second_session(symbol: str) -> tuple[ChartWindowResult, tuple[int, int]]:
    """Every 5 s bar of one regular session, priced like a four-digit stock
    quoted to the cent: the widest price strings a ``ChartBar`` realistically carries."""
    open_ms, close_ms = live_window(_FIVE_SECOND_SESSION_DAY_MS)
    bars = [
        IbkrMinuteBar(
            symbol=symbol,
            start_ms=start_ms,
            end_ms=start_ms + 5_000,
            open=Decimal("1234.56"),
            high=Decimal("1234.91"),
            low=Decimal("1233.87"),
            close=Decimal("1234.65"),
            volume=12_345,
            fetched_at_ms=start_ms + 5_000,
        )
        for start_ms in range(open_ms, close_ms, 5_000)
    ]
    result = ChartWindowResult(bars=bars, timeframe="5s", resolution="5s", feed=CHART_FEED_NOT_EXPECTED)
    return result, (open_ms, close_ms)


def _full_session_snapshot(panel: dict, *, panel_notice_bytes: int = 0) -> BotPanelLiveSnapshot:
    """A bot that listens to one asset and trades another (so its chart carries
    a position pane), over a whole session of 5 s bars, with the busiest panel
    its bounded lists allow: ``RUN_FILL_LIMIT`` fills, each simulated-priced.
    ``panel_notice_bytes`` pads the panel's own notice, to model a panel that
    has grown."""
    listened, window = _five_second_session("SPY")
    traded, _ = _five_second_session("QQQ")
    chart = build_live_chart(listened, [], strategy_instance_id=SID, symbol="SPY", window=window, now_ms=window[1])
    traded_chart = build_live_chart(traded, [], strategy_instance_id=SID, symbol="QQQ", window=window, now_ms=window[1])
    chart = chart.model_copy(update={"position": position_pane(chart, traded_chart)})
    fills = [
        RunFillView(
            filled_at_ms=window[0] + index * 60_000,
            side="buy" if index % 2 == 0 else "sell",
            quantity=10.0,
            price=512.34,
            symbol="SPY",
            order_ref=f"learn-ai/{SID}/v1:intent-{index:04d}",
            event_key=f"evt-{index:08d}-abcdef012345",
            simulated=SimulatedFillView(
                kind="bar_close",
                label="Priced at the minute's close",
                sentence="A Dry Run fill, priced at the closing price of the order's own minute.",
                price_age_ms=None,
                caution=False,
            ),
        )
        for index in range(RUN_FILL_LIMIT)
    ]
    view = BotPanelView.model_validate(panel).model_copy(
        update={"run_fills": fills, "experimental_notice": "x" * panel_notice_bytes or None}
    )
    return BotPanelLiveSnapshot(
        stream_epoch=f"{'a' * 32}:{'b' * 32}", surface_version=42, panel=view, live_chart=chart
    )


class _ReplayHub:
    """The producer side of one live stream: its one snapshot, then the end."""

    strategy_instance_id = SID
    is_available = True

    def __init__(self, snapshot: BotPanelLiveSnapshot) -> None:
        self._snapshot = snapshot
        self.stream_epoch = snapshot.stream_epoch
        self.surface_version = snapshot.surface_version

    def stall(self) -> None:
        return None

    def seconds_until_stall(self) -> None:
        return None

    def subscribe(self) -> asyncio.Queue[BotPanelLiveSnapshot | None]:
        queue: asyncio.Queue[BotPanelLiveSnapshot | None] = asyncio.Queue(maxsize=2)
        queue.put_nowait(self._snapshot)
        queue.put_nowait(None)
        return queue

    def unsubscribe(self, _queue: asyncio.Queue[BotPanelLiveSnapshot | None]) -> None:
        return None


async def _live_stream_through_the_coordinator(
    monkeypatch: pytest.MonkeyPatch, snapshot: BotPanelLiveSnapshot
) -> list[SseEvent]:
    """The router's real live stream for ``snapshot``, relayed as production
    relays it: through the lane's provenance injector, then the coordinator's
    own SSE parser at its production cap. A frame the parser refuses raises."""

    async def get_hub(*_args: object, **_kwargs: object) -> _ReplayHub:
        return _ReplayHub(snapshot)

    monkeypatch.setattr(broker_v2_panel, "get_or_start_live_projection_hub", get_hub)
    monkeypatch.setattr(broker_v2_panel, "retain_live_projection_hub", lambda *_a, **_k: None)
    monkeypatch.setattr(broker_v2_panel, "release_live_projection_hub", lambda *_a, **_k: None)
    response = await broker_v2_panel.stream_live_snapshot_scoped("alpaca", ACCT, SID, "5s", None)
    injector = _FrameInjector(
        lambda: {
            "broker": "alpaca",
            "clerk_id": "clrk_0123456789abcdef",
            "routing_epoch": 1_234,
            "binding_generation": 56,
        }
    )

    async def chunks() -> AsyncIterator[bytes]:
        async for frame in response.body_iterator:
            for injected in injector.push(frame.encode()):
                yield injected

    return [event async for event in iter_sse_events(chunks())]


async def _panel_json(app: FastAPI) -> dict:
    async with _client(app) as client:
        return (await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel")).json()


async def test_a_full_sessions_five_second_frame_reaches_the_coordinator_whole(
    api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The widest frame the bounded panel and a whole session of 5 s bars
    produce (with a position pane) fits the coordinator's parser as it is, so
    nothing is trimmed from the chart the operator reads."""
    app, _repo = api
    snapshot = _full_session_snapshot(await _panel_json(app))

    events = await asyncio.wait_for(
        _live_stream_through_the_coordinator(monkeypatch, snapshot), timeout=30.0
    )

    assert [event.event for event in events] == ["snapshot", "end"]
    document = json.loads(events[0].data)
    assert len(document["live_chart"]["bars"]) == len(snapshot.live_chart.bars) > 4_000
    assert len(document["live_chart"]["position"]["points"]) == len(snapshot.live_chart.bars)


async def test_a_live_frame_over_the_coordinators_cap_ends_the_stream_by_name(
    api, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A frame over the coordinator's per-event cap used to go out whole, and
    the coordinator's parser killed the stream after the 200 with no reason
    (the fault #2328 closed for the gallery). The frame is now sized before it
    is sent: the stream ends with a named ``error`` event the client already
    understands, and the lane logs why."""
    app, _repo = api
    # A panel that has grown a quarter megabyte past a full session's frame.
    snapshot = _full_session_snapshot(await _panel_json(app), panel_notice_bytes=250_000)
    assert len(snapshot.model_dump_json().encode()) > DEFAULT_MAX_EVENT_BYTES

    with caplog.at_level(logging.ERROR, logger=broker_v2_panel.__name__):
        events = await asyncio.wait_for(
            _live_stream_through_the_coordinator(monkeypatch, snapshot), timeout=30.0
        )

    assert [event.event for event in events] == ["error"]
    assert isinstance(json.loads(events[0].data)["error"], str)
    assert any(
        getattr(record, "action", None) == "live_stream_frame_over_cap" for record in caplog.records
    )


async def test_presented_action_executes_and_repost_replays_as_noop(api) -> None:
    """A repost of an applied action must never re-execute (2026-08-25 / F15).

    The SQLite executor now consults the same durable idempotency ledger as
    the shared executor, checked BEFORE the staleness fence: the retry of an
    applied action is a safe ``applied=False`` replay of the recorded
    receipt, not a 409 and not a second execution.
    """
    app, _repo = api
    async with _client(app) as client:
        panel = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel"
        )
        action = next(
            item for item in panel.json()["actions"] if item["action_id"] == "reconcile_now"
        )
        request = {
            "action_id": "reconcile_now",
            "concurrency_token": action["concurrency_token"],
            "idempotency_key": "sqlite-reconcile",
        }
        first = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/actions", json=request
        )
        second = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/actions", json=request
        )

    assert first.status_code == 200
    assert first.json()["applied"] is True
    assert second.status_code == 200
    assert second.json()["applied"] is False
    assert second.json()["receipt_id"] == first.json()["receipt_id"]


async def test_a_performer_failure_and_its_replay_never_put_its_exception_text_on_the_wire(
    api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1865: no exception text (a database path, say) in a response body, first attempt or replay.

    The first attempt is the typed unknown outcome; the replay of its burned
    key is a 409 that says the outcome is unknown, in the ledger's own words.
    """
    from app.services.broker_v2_panel import sqlite_panel_source

    app, _repo = api

    async def execute(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("secret /var/db/path")

    monkeypatch.setattr(sqlite_panel_source, "execute_recovery_action", execute)
    async with _client(app) as client:
        panel = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel")
        action = next(item for item in panel.json()["actions"] if item["action_id"] == "reconcile_now")
        request = {
            "action_id": "reconcile_now",
            "concurrency_token": action["concurrency_token"],
            "idempotency_key": "performer-fails",
        }
        first = await client.post(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/actions", json=request)
        replay = await client.post(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/actions", json=request)

    assert (first.status_code, first.json()["detail"]["outcome"]) == (500, "unknown")
    assert replay.status_code == 409, replay.text
    assert "outcome is unknown" in replay.json()["detail"]["why"]
    for response in (first, replay):
        assert "secret" not in response.text


async def test_a_presented_action_reaches_the_clerk_as_the_routes_configured_operator(
    api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator the route acts as travels with the action to the SQLite executor (#2854).

    An order an operator settles records who settled it, so the identity must
    arrive where the Clerk is asked: never a request field, always the
    route's configured one.
    """
    app, _repo = api
    acted_as: list[str | None] = []
    executor = broker_v2_panel.ds.execute_sqlite_panel_action

    async def execute(*args: object, **kwargs: object):
        acted_as.append(kwargs.get("operator_identity"))  # type: ignore[arg-type]
        return await executor(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(broker_v2_panel.ds, "execute_sqlite_panel_action", execute)
    monkeypatch.setattr(broker_v2_panel.settings, "PANEL_OPERATOR_IDENTITY", "desk-operator")
    async with _client(app) as client:
        panel = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel")
        action = next(item for item in panel.json()["actions"] if item["action_id"] == "reconcile_now")
        answered = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/actions/quiesce",
            json={
                "action_id": "reconcile_now",
                "concurrency_token": action["concurrency_token"],
                "idempotency_key": "who-asks",
            },
        )

    assert answered.status_code == 200, answered.text
    assert acted_as == ["desk-operator"]


async def test_action_refuses_malformed_sid_before_touching_receipt_path(
    api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed bot id is a clean 404 and never reaches the receipt path.

    The durable panel-action receipt ledger keys a filesystem path by the
    request's ``sid`` (CodeQL py/path-injection, PR #2550). The instance-id
    guard in ``run_action`` is the boundary copy of the artifact-path
    validators: it refuses the id before the roster or the receipt store is
    touched. Before the guard a malformed id surfaced as a harness 500.
    """
    from app.services.broker_v2_panel import panel_data_source

    app, _repo = api
    seen_paths: list[Path] = []
    real_store = panel_data_source.durable_idempotency_store_for

    def spy(artifacts_root: Path, sid: str) -> object:
        seen_paths.append(artifacts_root / sid)
        return real_store(artifacts_root, sid)

    monkeypatch.setattr(panel_data_source, "durable_idempotency_store_for", spy)
    request = {
        "action_id": "stop_bot_decisions",
        "concurrency_token": "token",
        "idempotency_key": "malformed-sid",
    }
    async with _client(app) as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/evil%20id/actions", json=request
        )

    assert response.status_code == 404
    assert seen_paths == []


async def test_chart_contract_rejects_unknown_timeframe(api) -> None:
    app, _repo = api
    async with _client(app) as client:
        history = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/chart/history",
            params={"timeframe": "5m"},
        )

    assert history.status_code == 422


async def test_a_roster_read_while_the_runner_is_not_up_refuses_instead_of_listing_part_of_it(api) -> None:
    """Dry Run and rehearsal rows come from the runner: without it the roster is not partial, it is unavailable."""
    app, _repo = api
    set_bot_task_registry(None)

    async with _client(app) as client:
        roster = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/catalog")

    assert roster.status_code == 503, roster.text
    assert roster.json()["detail"]["message"] == "The bot runner is not available."


@pytest.mark.parametrize("surface", ["panel", "evidence", "live-snapshot", "live-stream"])
@pytest.mark.parametrize("sid", ["%2E%2E", "%20padded"], ids=["traversal-segment", "padded"])
async def test_a_malformed_bot_id_is_a_404_on_every_bot_read_not_a_503(api, surface: str, sid: str) -> None:
    """The panel and its actions already answer an id no bot could have as 404; so do the
    evidence page and the live reads, which used to say the service was unavailable."""
    app, _repo = api

    async with _client(app) as client:
        response = await client.get(f"{_ACCOUNT_ROOT}/bots/{sid}/{surface}")

    assert response.status_code == 404, response.text
    assert "not a valid strategy instance id" in response.json()["detail"]["message"]


@pytest.fixture()
def budget_api(api):
    """The panel harness with its account authority serving the money reads, as an account that has not switched to budgets."""
    app, repo = api
    runtime = get_active_clerk_runtime()
    assert runtime is not None
    runtime._sqlite_repository = repo
    runtime.account_authority_kind = "real_paper"
    return app, repo


@pytest.mark.parametrize("sid", ["%2E%2E", "%20padded"], ids=["traversal-segment", "padded"])
async def test_a_malformed_bot_id_is_a_404_on_the_budget_read_not_a_500(budget_api, sid: str) -> None:
    app, _repo = budget_api

    async with _client(app) as client:
        response = await client.get(f"{_ACCOUNT_ROOT}/bots/{sid}/budget")

    assert response.status_code == 404, response.text
    assert "not a valid strategy instance id" in response.json()["detail"]["message"]


async def test_an_unknown_bot_has_no_budget_to_read_and_an_earlier_deployment_reads_legacy(budget_api) -> None:
    """A bot nothing holds is a 404; a bot the runner holds that was deployed before budgets still says so."""
    app, repo = budget_api

    async with _client(app) as client:
        unknown = await client.get(f"{_ACCOUNT_ROOT}/bots/never-deployed/budget")
        earlier = await client.get(f"{_ACCOUNT_ROOT}/bots/{SID}/budget")

    assert unknown.status_code == 404, unknown.text
    assert unknown.json()["detail"]["message"] == "No bot 'never-deployed' is bound to broker 'alpaca'."
    assert repo.strategy_instance("never-deployed") is None and repo.deployment_budget("never-deployed") is None
    assert earlier.status_code == 200, earlier.text
    assert earlier.json()["state"] == "legacy"


async def test_a_malformed_evidence_cursor_is_a_400_not_a_500(api) -> None:
    """A cursor the timeline cannot read is the client's to drop, as on the Clerk's own timeline route."""
    app, _repo = api

    async with _client(app) as client:
        response = await client.get(
            f"{_ACCOUNT_ROOT}/bots/{SID}/evidence", params={"cursor": "not-a-cursor"}
        )

    assert response.status_code == 400, response.text
    assert response.json()["detail"]["message"] == "The evidence cursor is not valid."


async def test_a_run_window_read_while_the_runner_is_not_up_refuses_with_a_503(api) -> None:
    """The run window is the runner's record of the run; with no runner the read was an AttributeError 500."""
    app, _repo = api
    set_bot_task_registry(None)

    async with _client(app) as client:
        tape = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/chart/history",
            params={"timeframe": "1m", "from_ms": _T0 - 600_000, "to_ms": _T0},
        )

    assert tape.status_code == 503, tape.text
    assert tape.json()["detail"]["message"] == "The bot runner is not available."


async def test_a_finished_runs_tape_reads_that_runs_window_and_refuses_another(
    api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2794: a finished run's Tape · 1m shows that run, never today's newest bars."""
    from app.schemas.broker_v2_panel import ChartBar
    from app.schemas.fleet_history_batch import HistoryBatchQuery, HistoryBatchResponse

    app, _repo = api
    get_bot_task_registry()._running = False  # type: ignore[union-attr]
    asked: list[HistoryBatchQuery] = []

    async def provider(query: HistoryBatchQuery) -> HistoryBatchResponse:
        asked.append(query)
        bars = [
            ChartBar(start_ms=start, end_ms=start + 60_000, open="1", high="1", low="1", close="1", volume=1,
                     source="polygon")
            for start in range(query.as_of_ms - query.required_bar_count * 60_000, query.as_of_ms, 60_000)
        ]
        return HistoryBatchResponse(bars=bars, source="polygon", overlay_notices=[], effective_as_of_ms=query.as_of_ms)

    monkeypatch.setattr(panel_chart_data_source, "build_history_batch_provider", lambda: provider)
    url = f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/chart/history"
    async with _client(app) as client:
        run = await client.get(url, params={"timeframe": "1m", "from_ms": _T0 - 600_000, "to_ms": _T0 + 120_000})
        elsewhere = await client.get(url, params={"timeframe": "1m", "from_ms": _T0 - 86_400_000, "to_ms": _T0})
        half = await client.get(url, params={"timeframe": "1m", "from_ms": _T0})
        daily = await client.get(url, params={"timeframe": "1d", "from_ms": _T0 - 600_000, "to_ms": _T0 + 120_000})

    assert run.status_code == 200, run.text
    body = run.json()
    assert (body["from_ms"], body["to_ms"]) == (_T0 - 600_000, _T0 + 120_000)
    starts = [bar["start_ms"] for bar in body["bars"]]
    assert starts == sorted(set(starts)) and starts[0] == _T0 - 600_000 and len(starts) == 12
    assert asked[0].as_of_ms == _T0 + 120_000
    assert elsewhere.status_code == 422
    assert elsewhere.json()["detail"]["message"] == (
        "The window must lie within this bot's latest run, give or take 30 minutes."
    )
    assert (half.status_code, half.json()["detail"]["message"]) == (422, "A run window needs both from_ms and to_ms.")
    # A daily bar starts at midnight, before any run window: refused, never an empty tape with no notice.
    assert (daily.status_code, daily.json()["detail"]["message"]) == (
        422,
        "A run window is drawn on 1-minute bars; ask for the 1m timeframe.",
    )


async def test_lost_execution_lease_is_an_authored_blocker_not_a_raw_500(
    api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T7c (#1794): a frozen process past its lease TTL bricked the surface.

    On 2026-08-26 a ~50 s SIGSTOP let the execution lease expire; on resume
    every panel action returned a raw 500 leaking the internal handle
    message, with no operator-authored cure. Fail-closed is correct -- a
    holder that lost its lease must not write -- but the surface has to say
    so. The clerk router already translated this exception; the panel router
    had no handler for it at all.
    """
    app, _repo = api

    def _lease_lost(*_args: object, **_kwargs: object) -> None:
        raise ExecutionLeaseLost(
            f"account {ACCT!r} execution lease was lost or expired; "
            "this handle can no longer write"
        )

    async with _client(app) as client:
        panel = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel"
        )
        action = next(
            item for item in panel.json()["actions"] if item["action_id"] == "reconcile_now"
        )
        request = {
            "action_id": "reconcile_now",
            "concurrency_token": action["concurrency_token"],
            "idempotency_key": "sqlite-lease-lost",
        }
        # Patch beneath the translating seam so the translation itself runs.
        monkeypatch.setattr(
            broker_v2_panel.ds,
            "_run_action_under_live_authority",
            _lease_lost,
            raising=True,
        )
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/actions", json=request
        )

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["reason_code"] == "EXECUTION_LEASE_LOST"
    assert detail["outcome"] == "failure"

    # Authored copy, not the internal handle message.
    rendered = json.dumps(detail)
    assert "can no longer write" not in rendered
    assert "handle" not in rendered
    assert "restart" in detail["why"].lower()


async def test_write_path_revives_an_expired_lease_and_reports_a_retryable_refusal(
    lease_lost_api,
) -> None:
    """A process frozen past its lease TTL and then thawed, with nobody else
    ever touching the account, self-cures on the very action that discovers
    the loss -- exactly like the lease heartbeat already self-cures it.
    Before ADR 0050, the write path had no revival attempt at all: every
    mutating action after a thaw (Stop, Resume, Retire, flatten,
    reconcile_now, cohort flatten) went straight to the terminal "restart the
    data plane" blocker, even during the ADR 0050 self-cure window.

    See ADR 0050's 2026-09-06 addendum for why this reports a retryable
    refusal instead of auto-retrying the mutation in-process: nothing was
    applied under the revived lease, and only a second, genuinely fresh
    request (a fresh idempotency key, exactly like the panel client mints
    per submission) reaches the broker.
    """
    app, repo, clock, hook_calls = lease_lost_api
    clock.advance(5_000)  # freeze past the 1 s TTL; nobody else takes the lease

    response = await _post_stop_bot_decisions(app, idempotency_key="lease-revival-success")

    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert detail["reason_code"] == "EXECUTION_LEASE_REVIVED"
    # "conflict", not "failure": nothing applied and a re-POST covers it --
    # the same retryable reading cohort_execution.py's classification gives
    # this error ("refused" -> "conflict"), aligned here (#1955 final review).
    assert detail["outcome"] == "conflict"

    # The copy reports a self-cured lease and a retry, never the terminal
    # restart cure -- that copy is reserved for a revival the store refuses.
    rendered = json.dumps(detail)
    assert "restart" not in rendered.lower()
    assert "revived" in rendered.lower()

    # Nothing applied: the bot is still running, the action stays enabled.
    registry = get_bot_task_registry()
    assert registry is not None and registry._running is True  # type: ignore[attr-defined]

    # ADR 0050 §3: the revival went through ReconciliationSweep.revive_now()
    # -- the same entry point the lease heartbeat uses -- so the post-revival
    # recovery hook fired exactly once, even though this request's own
    # mutation was never retried.
    assert len(hook_calls) == 1

    # The operator's next click is a genuinely fresh request (a fresh
    # idempotency key -- the panel client mints one per submission) and
    # reaches the now-revived lease.
    retry = await _post_stop_bot_decisions(app, idempotency_key="lease-revival-operator-retry")

    assert retry.status_code == 200, retry.text
    assert retry.json()["applied"] is True
    repo.renew_execution_lease()
    assert registry._running is False  # type: ignore[attr-defined]

    # The revival is not re-triggered by the second request: the hook still
    # fired exactly once for the whole scenario.
    assert len(hook_calls) == 1


async def test_write_path_does_not_revive_the_account_for_a_dry_run_authoritys_lost_lease(
    lease_lost_api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Dry Run bot's performers write through its isolated ``sim:`` Clerk,
    so the lost lease can be the synthetic authority's while ``account_id``
    is the real operator account. The write path must not revive the real
    account on that bot's behalf: no recovery hook for an unrelated
    authority, no "revived" report about a lease it never touched.
    """
    app, repo, _clock, hook_calls = lease_lost_api
    lease_before = repo._conn.execute(
        "SELECT execution_lease_expires_at_ms FROM control_meta WHERE id = 1"
    ).fetchone()[0]

    def _synthetic_lease_lost(*_args: object, **_kwargs: object) -> None:
        raise ExecutionLeaseLost(
            f"account 'sim:{SID}' execution lease was lost or expired; "
            "this handle can no longer write",
            account_id=f"sim:{SID}",
        )

    monkeypatch.setattr(
        broker_v2_panel.ds, "_run_action_under_live_authority", _synthetic_lease_lost, raising=True
    )
    response = await _post_stop_bot_decisions(app, idempotency_key="sim-lease-lost")

    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert detail["reason_code"] == "EXECUTION_LEASE_LOST"
    # Retryable, like a revived lease: the bot's own synthetic heartbeat
    # revives it after a thaw, and nothing was applied.
    assert detail["outcome"] == "conflict"
    assert "Dry Run" in detail["why"]
    # Nothing was attempted against the account: its hook never fired and
    # its lease expiry is exactly what it was.
    assert hook_calls == []
    lease_after = repo._conn.execute(
        "SELECT execution_lease_expires_at_ms FROM control_meta WHERE id = 1"
    ).fetchone()[0]
    assert lease_after == lease_before


async def test_write_path_keeps_the_restart_cure_when_revival_is_refused(
    lease_lost_api,
) -> None:
    """ADR 0050's other half: when the store proves another writer (or a
    reset ceremony) actually touched the account while this handle's lease
    was expired, the write path's own revival attempt must be refused too --
    the terminal restart cure stays correct. The copy must give the store's
    refusal reason and must not claim "no control on this panel can
    re-acquire it": this panel action just tried, and the store said no.
    """
    app, repo, clock, hook_calls = lease_lost_api
    repo._conn.execute(
        "UPDATE control_meta SET execution_lease_owner = 'someone-else', "
        "execution_lease_expires_at_ms = ? WHERE id = 1",
        (repo.clock() + 1,),
    )
    repo._conn.commit()
    clock.advance(5_000)  # the usurper's lease has itself lapsed too

    response = await _post_stop_bot_decisions(app, idempotency_key="lease-revival-refused")

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["reason_code"] == "EXECUTION_LEASE_LOST"

    rendered = json.dumps(detail)
    assert "can no longer write" not in rendered
    assert "handle" not in rendered
    assert "restart" in detail["why"].lower()
    assert "no control on this panel can re-acquire it" not in detail["why"].lower()
    assert "another writer" in detail["why"].lower()

    # The refused CAS never reached the success branch, so the post-revival
    # recovery hook must not have fired.
    assert hook_calls == []


async def test_write_path_translates_a_repository_poisoned_by_revive_now(
    lease_lost_api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """revive_now()'s own admission check (``_assert_not_poisoned``) can find
    the repository poisoned by an unconfirmed mirror finalize from before the
    freeze -- a lease lost AND poisoned after one freeze is not a made-up
    edge case, it is the same freeze producing both symptoms. This must
    translate to ``AuthorityPoisonedError`` (a different cure than a lost
    lease -- fence reconciliation, not a lease timeout), never an unhandled
    500 leaking internal remediation text.
    """
    app, _repo, clock, hook_calls = lease_lost_api
    clock.advance(5_000)  # freeze past the 1 s TTL

    runtime = get_active_clerk_runtime()
    assert runtime is not None and runtime.sweep is not None

    async def _poisoned_revive_now() -> bool:
        raise RepositoryPoisoned(
            "account repository is poisoned after an unconfirmed mirror finalize"
        )

    monkeypatch.setattr(runtime.sweep, "revive_now", _poisoned_revive_now)

    response = await _post_stop_bot_decisions(app, idempotency_key="lease-revival-poisoned")

    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert detail["reason_code"] == "AUTHORITY_MIRROR_UNCONFIRMED"

    # Never reported as a lost lease -- the cures differ.
    rendered = json.dumps(detail)
    assert "EXECUTION_LEASE" not in rendered

    # The CAS never confirmed a revival, so the post-revival hook never ran.
    assert hook_calls == []


async def test_write_path_reports_a_transient_store_error_as_a_typed_blocker(
    lease_lost_api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A store error other than ``ExecutionLeaseLost``/``RepositoryPoisoned``
    (e.g. a locked SQLite file mid-CAS) never reached an ``except`` clause in
    rounds 1 and 2 and leaked as a raw 500 with internal exception text. The
    CAS never reached a confirmed outcome -- unknown, not proven-lost -- so
    this reports a typed, transient blocker mirroring the lease heartbeat's
    own "errored; retrying" vocabulary, never a bare 500.
    """
    app, _repo, clock, hook_calls = lease_lost_api
    clock.advance(5_000)  # freeze past the 1 s TTL

    runtime = get_active_clerk_runtime()
    assert runtime is not None and runtime.sweep is not None

    async def _locked_revive_now() -> bool:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(runtime.sweep, "revive_now", _locked_revive_now)

    response = await _post_stop_bot_decisions(app, idempotency_key="lease-revival-transient")

    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert detail["reason_code"] == "EXECUTION_LEASE_LOST"

    # Distinct from a store-proven refusal: no claim another writer took the
    # account, and the internal exception text never leaks to the surface.
    rendered = json.dumps(detail)
    assert "database is locked" not in rendered
    assert "another writer" not in rendered.lower()
    assert "retry" in detail["why"].lower()

    # The CAS never confirmed a revival, so the post-revival hook never ran.
    assert hook_calls == []


async def test_poisoned_authority_is_not_reported_as_a_lost_lease(
    api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two fail-closed conditions have different cures (CodeRabbit, #1794).

    `RepositoryPoisoned` means a transition committed but its mirror finalize
    was unconfirmed -- this authority's own durability is unproven until the
    exact fence reconciliation re-runs. A lost lease means another process owns
    the account. Reporting the first as the second would send an operator to
    wait out a lease timeout that is not the problem.
    """
    app, _repo = api

    def _poisoned(*_args: object, **_kwargs: object) -> None:
        raise RepositoryPoisoned("mirror finalize unconfirmed for transition 42")

    async with _client(app) as client:
        panel = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel"
        )
        action = next(
            item for item in panel.json()["actions"] if item["action_id"] == "reconcile_now"
        )
        monkeypatch.setattr(
            broker_v2_panel.ds,
            "_run_action_under_live_authority",
            _poisoned,
            raising=True,
        )
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/actions",
            json={
                "action_id": "reconcile_now",
                "concurrency_token": action["concurrency_token"],
                "idempotency_key": "sqlite-poisoned",
            },
        )

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["reason_code"] == "AUTHORITY_MIRROR_UNCONFIRMED"
    assert detail["reason_code"] != "EXECUTION_LEASE_LOST"

    # Names the actual condition, and never the lease-timeout remedy.
    rendered = json.dumps(detail).lower()
    assert "mirror" in rendered
    assert "lease" not in rendered


async def test_exhausted_panel_projection_refuses_and_says_so(
    api,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """T5 (#1796): exhaustion stays fail-closed, and is no longer silent.

    A torn cut is still refused rather than spliced. What changed is that
    exhausting the attempts now leaves a structured record -- without one
    there is no way to tell a load-correlated blip from custody churning
    faster than the projection can ever settle, since the 503 looks
    identical either way.
    """
    app, repo = api
    base = repo.control_meta_snapshot()
    churn = iter(range(base.control_revision + 1, base.control_revision + 200))
    monkeypatch.setattr(
        repo,
        "control_meta_snapshot",
        lambda: replace(base, control_revision=next(churn)),
    )

    with caplog.at_level(logging.WARNING):
        async with _client(app) as client:
            response = await client.get(
                f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel"
            )

    assert response.status_code == 503
    assert any(
        getattr(record, "action", None) == "sqlite_panel_projection_torn_read_exhausted"
        for record in caplog.records
    ), "exhausting the coherence attempts must leave a structured record"


@pytest.mark.asyncio
async def test_dry_run_panel_pulse_uses_selected_clerk_window_without_primary(api, monkeypatch):
    from contextlib import asynccontextmanager
    from datetime import datetime
    from decimal import Decimal
    from zoneinfo import ZoneInfo

    from app.broker.alpaca.broker import ALPACA_EXTENDED_HOURS_WINDOW
    from app.broker.alpaca.clerk.program_leg import ProgramLegPolicy
    from app.broker.alpaca.marketable_limit import ExtendedHoursAllowances
    from app.services.bot_start_admission import market_data_admission_fact
    from app.utils.timestamps import to_ms_utc

    _app, _repo = api
    runtime = get_active_clerk_runtime()
    facade = runtime.clerk
    policy = ProgramLegPolicy(window=ALPACA_EXTENDED_HOURS_WINDOW,
                              allowances=ExtendedHoursAllowances(entry_bps=Decimal("10"), exit_bps=Decimal("20")))
    facade._program_leg_policy = policy
    registry = get_bot_task_registry()
    binding = registry.binding_for_control("alpaca", SID)
    binding.mode, binding.use_rth = "dry_run", False
    binding.quantity, binding.carryover_policy = 1, "FORBID"
    monkeypatch.setattr(registry, "binding_for_control", lambda *_: binding)

    @asynccontextmanager
    async def synthetic_runtime_for_projection(_binding):
        yield ActiveClerkRuntime(authority_kind="synthetic", clerk=facade)

    monkeypatch.setattr(registry, "synthetic_runtime_for_projection", synthetic_runtime_for_projection, raising=False)
    now_ms = to_ms_utc(datetime(2026, 9, 2, 8, tzinfo=ZoneInfo("America/New_York")))
    monkeypatch.setattr(panel_data_source, "now_ms_utc", lambda: now_ms)
    clerk_status = await panel_data_source.clerk_status(symbol="SPY")

    async def projected_clerk_status(**_kwargs):
        return clerk_status

    monkeypatch.setattr(panel_data_source, "clerk_status", projected_clerk_status)
    set_active_clerk_runtime(None)
    admission = market_data_admission_fact(None, now_ms, symbol="SPY", use_rth=False,
                                          extended_window=policy.window)
    assert (admission.scheduled_phase, admission.session_authority_source) == ("PRE", "broker_declared_window")
    async with panel_data_source._panel_authority_for_binding(registry, binding) as selected:
        panel, _ = await panel_data_source._get_panel_with_entries_from_authority(
            "alpaca", ACCT, SID, resolved=ACCT, captured_now_ms=now_ms,
            registry=registry, binding=binding, facade=selected,
        )
    assert panel.market_pulse.session == "PRE_MARKET"


def _dv_explanation(end_ms: int) -> dict:
    """A Deployment Validation decision explanation, as the runner stores it (#2639)."""
    return DecisionExplanationRecord(
        bar=DecisionBarRecord(
            start_ms=end_ms - 60_000, end_ms=end_ms, open=100.0, high=101.0, low=99.5, close=100.5, volume=900.0
        ),
        ready=True,
        holding=False,
        signal="HOLD",
        values={},
        checks=[
            ExplainedCheckRecord(
                check_id="in_window", role="entry", comparison="state", passed=True, observed="inside"
            ),
            ExplainedCheckRecord(
                check_id="green_streak", role="entry", comparison="ge", passed=False, observed=1, threshold=2
            ),
        ],
    ).model_dump(mode="json")


def _append_no_action(repo: ClerkSqliteRepository, seq_ms: int, *, explanation: dict | None) -> None:
    journal = SqliteDecisionReceipts(repo, strategy_instance_id=SID)
    facts: dict = {
        "bar_ref": f"decision-bar:ibkr:SPY:{seq_ms}",
        "decision_id": f"{seq_ms:064x}"[-64:],
        "run_id": _run_id(SID),
        "reason_code": "NO_ACTION",
        "decision_bar_close_ms": seq_ms,
    }
    if explanation is not None:
        facts["explanation"] = explanation
    journal.append(outcome="no_action", symbol="SPY", observed_at_ms=seq_ms + 5, facts=facts)


async def test_strategy_view_draws_the_bots_own_decisions(api) -> None:
    app, repo = api
    _append_no_action(repo, _T0, explanation=None)
    _append_no_action(repo, _T0 + 60_000, explanation=_dv_explanation(_T0 + 60_000))

    async with _client(app) as client:
        response = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/strategy-view")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["strategy_key"] == "deployment_validation"
    assert body["declaration"]["gates"] == [
        {
            "gate_id": "in_window",
            "label": "Inside its trading window",
            "expression": "open + 15 min ≤ bar close < close − 15 min",
            "source": "strategy",
        }
    ]
    [candle] = body["candles"]
    assert (candle["bar_close_ms"], candle["phase"], candle["outcome"]) == (_T0 + 60_000, "decision", "no_action")
    assert candle["gates"] == {"in_window": True}
    assert [(c["label"], c["observed_text"], c["needs"], c["passed"]) for c in candle["explanation"]["checks"]] == [
        ("Trading window", "inside", "open + 15 min to close − 15 min", True),
        ("Green bars in a row", "1", "≥ 2", False),
    ]
    # The decision recorded before values were saved is counted, never drawn.
    assert body["unexplained_decision_count"] == 1


async def test_panel_decision_rows_explain_themselves(api) -> None:
    app, repo = api
    _append_no_action(repo, _T0, explanation=None)
    _append_no_action(repo, _T0 + 60_000, explanation=_dv_explanation(_T0 + 60_000))

    async with _client(app) as client:
        response = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel")

    assert response.status_code == 200, response.text
    newest, older = response.json()["recent_decisions"][:2]
    assert newest["decision_bar_close_ms"] == _T0 + 60_000
    assert [chip["chip"] for chip in newest["explanation"]["checks"]] == ["window", "green 1"]
    # "values not recorded": a row from before decisions saved them.
    assert older["explanation"] is None


def _append_receipt_without_a_reason(repo: ClerkSqliteRepository, seq_ms: int) -> None:
    """A receipt whose facts lack ``reason_code``: stored, but not a panel decision row."""
    SqliteDecisionReceipts(repo, strategy_instance_id=SID).append(
        outcome="no_action",
        symbol="SPY",
        observed_at_ms=seq_ms + 5,
        facts={"bar_ref": f"decision-bar:ibkr:SPY:{seq_ms}"},
    )


async def test_one_unreadable_decision_receipt_is_its_own_row_and_never_takes_the_page_or_its_actions_down(
    api,
) -> None:
    """A decision row is display evidence; the page and its controls are the operator's.

    One receipt among the bot's last eight that this build cannot read is
    shown as unreadable, in its place, while every other row and every action
    on the page keeps working.
    """
    app, repo = api
    _append_no_action(repo, _T0, explanation=None)
    _append_receipt_without_a_reason(repo, _T0 + 60_000)
    _append_no_action(repo, _T0 + 120_000, explanation=None)

    async with _client(app) as client:
        panel = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel")
        action = next(item for item in panel.json()["actions"] if item["action_id"] == "reconcile_now")
        acted = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/actions",
            json={
                "action_id": "reconcile_now",
                "concurrency_token": action["concurrency_token"],
                "idempotency_key": "with-an-unreadable-receipt",
            },
        )

    assert panel.status_code == 200, panel.text
    assert [(row["reason_code"], row["bar_ref"] == "") for row in panel.json()["recent_decisions"]] == [
        ("NO_ACTION", False),
        ("RECEIPT_UNREADABLE", True),
        ("NO_ACTION", False),
    ]
    assert acted.status_code == 200, acted.text
    assert acted.json()["applied"] is True


async def test_a_receipt_whose_outcome_this_build_cannot_name_is_left_out_not_fatal(api) -> None:
    """A row from a newer build is reported and left out, like an explanation this build cannot read."""
    app, repo = api
    _append_no_action(repo, _T0, explanation=None)
    repo._conn.execute(
        "INSERT INTO decision_receipts (strategy_instance_id, seq, outcome, symbol, intent_id, order_ref, "
        "observed_at_ms, facts_json) VALUES (?, 2, 'from_a_newer_build', 'SPY', NULL, NULL, ?, '{}')",
        (SID, _T0 + 60_000),
    )
    repo._conn.commit()

    async with _client(app) as client:
        panel = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel")

    assert panel.status_code == 200, panel.text
    assert [row["reason_code"] for row in panel.json()["recent_decisions"]] == ["NO_ACTION"]


async def test_an_incoherent_custody_and_economics_cut_is_a_typed_503_on_the_page_and_its_actions(
    api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The adapter refuses a cut whose custody and economics are of two revisions, and says so.

    The page and the action POST (which builds the panel first) used to answer
    the global 500 for it: a bare ``RuntimeError`` nothing translated.
    """
    app, _repo = api
    async with _client(app) as client:
        panel = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel")
        action = next(item for item in panel.json()["actions"] if item["action_id"] == "reconcile_now")
        real_read = panel_data_source.read_sqlite_panel_evidence

        async def torn(*args: object, **kwargs: object):
            evidence = await real_read(*args, **kwargs)  # type: ignore[arg-type]
            snapshot = replace(
                evidence.economics.snapshot, control_revision=evidence.projection.control_revision + 1
            )
            return replace(evidence, economics=replace(evidence.economics, snapshot=snapshot))

        monkeypatch.setattr(panel_data_source, "read_sqlite_panel_evidence", torn)
        page = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel")
        acted = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/actions",
            json={
                "action_id": "reconcile_now",
                "concurrency_token": action["concurrency_token"],
                "idempotency_key": "on-an-incoherent-cut",
            },
        )

    for refused in (page, acted):
        assert refused.status_code == 503, refused.text
        assert refused.json()["detail"]["message"]
        assert "authority revision" in refused.json()["detail"]["why"]


async def test_a_live_accounts_panel_labels_its_decisions_real_live(
    api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2823: the served panel takes the world from the Clerk's learned mode, never a paper default."""
    app, repo = api
    _append_no_action(repo, _T0, explanation=None)
    monkeypatch.setattr(get_active_clerk_runtime().clerk, "_account_mode", "live")

    async with _client(app) as client:
        response = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel")

    assert response.status_code == 200, response.text
    decisions = response.json()["recent_decisions"]
    assert [(row["authority_account_id"], row["authority_kind"]) for row in decisions] == [(ACCT, "real_live")]


async def test_saved_settings_that_no_longer_validate_never_take_the_panel_down(
    api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2639: the panel is the operator's control surface; the strategy view is a display add-on."""
    app, repo = api
    _append_no_action(repo, _T0 + 60_000, explanation=_dv_explanation(_T0 + 60_000))
    registry = get_bot_task_registry()
    binding_for_control = registry.binding_for_control  # type: ignore[union-attr]

    def stale_binding(broker: str, sid: str) -> SimpleNamespace:
        # Longer than any symbol the strategy's schema accepts.
        return SimpleNamespace(**{**vars(binding_for_control(broker, sid)), "symbol": "S" * 21})

    monkeypatch.setattr(registry, "binding_for_control", stale_binding)

    async with _client(app) as client:
        panel = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel")
        view = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/strategy-view")

    assert panel.status_code == 200, panel.text
    assert panel.json()["recent_decisions"][0]["explanation"] is None
    assert view.status_code == 503, view.text


async def test_a_setting_the_strategy_has_since_retired_keeps_the_bots_explanations_and_view(
    api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2912: a bot deployed while deployment-validation still had its ``trade_symbol`` setting."""
    app, repo = api
    _append_no_action(repo, _T0 + 60_000, explanation=_dv_explanation(_T0 + 60_000))
    registry = get_bot_task_registry()
    binding_for_control = registry.binding_for_control  # type: ignore[union-attr]

    def earlier_binding(broker: str, sid: str) -> SimpleNamespace:
        return SimpleNamespace(**{**vars(binding_for_control(broker, sid)), "strategy_params": {"trade_symbol": "SPY"}})

    monkeypatch.setattr(registry, "binding_for_control", earlier_binding)

    async with _client(app) as client:
        panel = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel")
        view = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/strategy-view")

    assert panel.status_code == 200, panel.text
    assert panel.json()["recent_decisions"][0]["explanation"] is not None
    assert view.status_code == 200, view.text


# ── The bot page's lead (#2794) ──────────────────────────────────────────────


async def test_a_running_bot_page_leads_with_its_own_status_summary_and_toolbar(api) -> None:
    app, repo = api
    _append_no_action(repo, _T0 + 60_000, explanation=None)
    _append_no_action(repo, _T0 + 120_000, explanation=None)

    async with _client(app) as client:
        body = (await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel")).json()

    page = body["bot_page"]
    assert page["status"] == {"state": "running", "label": "Running", "reason": None}
    assert page["summary"]["text"] == (
        "Running since Tue Nov 14 2023, 17:13 ET · 2 decisions, no trades · $1000000.00 set aside."
    )
    assert page["summary"]["template_version"] == 2
    toolbar = {entry["action_id"]: entry for entry in page["toolbar"]}
    assert (toolbar["stop_bot_decisions"]["availability"], toolbar["stop_bot_decisions"]["primary"]) == (
        "available",
        True,
    )
    # Nothing held, no order that can fill, nothing missing: not needed, in the page's words.
    assert (toolbar["prepare_safe_flatten"]["availability"], toolbar["prepare_safe_flatten"]["reason"]) == (
        "not_needed",
        "This bot holds no shares.",
    )
    assert toolbar["cancel_verified_working_orders"]["availability"] == "not_needed"
    assert toolbar["discharge_attributed_residue"]["availability"] == "not_needed"
    assert toolbar["deploy_again"]["availability"] == "not_needed"
    assert toolbar["reconcile_now"]["label"] == "Check against Alpaca"
    assert [line["key"] for line in page["health"]["run"]] == ["feed", "decisions", "orders"]
    assert body["run_fills"] == []


async def test_an_order_that_can_still_fill_keeps_cancel_needed_though_blocked(api) -> None:
    """An order the broker has not acknowledged can still fill: Cancel is blocked, never "not needed"."""
    app, repo = api
    accept_budgeted_test_enter(
        repo, account_id=ACCT, strategy_instance_id=SID, decision_id="dec-1",
        lifecycle_run_id=_run_id(SID), leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
    )

    async with _client(app) as client:
        body = (await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel")).json()

    cancel = next(entry for entry in body["bot_page"]["toolbar"] if entry["action_id"] == "cancel_verified_working_orders")
    assert cancel["availability"] == "blocked"
    assert cancel["reason"] == "No working order has both a durable Clerk reference and broker identity."


async def test_an_order_the_broker_refused_leaves_nothing_to_cancel(api) -> None:
    """A refused order has ended: Cancel is not needed, however long ago it was refused."""
    app, repo = api
    refused = await submit_budgeted_test_enter(
        repo, account_id=ACCT, strategy_instance_id=SID, decision_id="dec-1", lifecycle_run_id=_run_id(SID),
        leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=1),
        trade=_FakeTradePort(submit_error=BrokerOrderRejected("insufficient buying power")),
    )
    assert refused.order_ref is not None

    async with _client(app) as client:
        body = (await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel")).json()

    cancel = next(entry for entry in body["bot_page"]["toolbar"] if entry["action_id"] == "cancel_verified_working_orders")
    assert (cancel["availability"], cancel["reason"]) == ("not_needed", "This bot has no open orders.")


async def test_an_account_hold_never_turns_a_finished_bot_red(api) -> None:
    """An old unexplained order is the account's: it shows in account health, not on the bot."""
    app, repo = api
    submit_stop_run(repo, account_id=ACCT, strategy_instance_id=SID, lifecycle_run_id=_run_id(SID))
    get_bot_task_registry()._running = False  # type: ignore[union-attr]
    raise_account_hold(repo, reason_code="UNEXPLAINED_ORDER_HOLD", evidence_refs=["bo-1"])

    async with _client(app) as client:
        body = (await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel")).json()

    # The account-scoped verdict still says blocked; the bot's own status does not.
    assert body["mission_verdict"]["state"] == "blocked"
    page = body["bot_page"]
    assert page["status"]["state"] == "finished"
    assert page["summary"]["text"] == (
        "Ran Tue Nov 14 2023, 17:13–17:14 ET · stopped · no decisions, no trades"
        " · $1000000.00 back to the account."
    )
    holds = next(line for line in page["health"]["account"] if line["key"] == "holds")
    assert (holds["state"], holds["value"], holds["note"]) == (
        "attention",
        "Unexplained-order hold",
        "No effect on this stopped bot.",
    )
    toolbar = {entry["action_id"]: entry for entry in page["toolbar"]}
    assert (toolbar["deploy_again"]["availability"], toolbar["deploy_again"]["primary"]) == ("available", True)
    assert (toolbar["stop_bot_decisions"]["availability"], toolbar["stop_bot_decisions"]["reason"]) == (
        "not_needed",
        "This bot is not running.",
    )
    assert toolbar["discharge_attributed_residue"]["availability"] == "not_needed"


async def test_a_bot_that_ended_holding_says_so_and_lists_its_runs_fill(api) -> None:
    app, repo = api
    await _make_held_position(repo, account_id=ACCT, strategy_instance_id=SID, run_id=_run_id(SID), quantity=1.0)
    submit_stop_run(repo, account_id=ACCT, strategy_instance_id=SID, lifecycle_run_id=_run_id(SID))
    get_bot_task_registry()._running = False  # type: ignore[union-attr]

    async with _client(app) as client:
        body = (await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel")).json()

    page = body["bot_page"]
    assert page["status"]["state"] == "ended_holding"
    assert page["summary"]["facts"]["held"] == [{"symbol": "SPY", "quantity": "1"}]
    assert page["summary"]["facts"]["trade_count"] == 1
    assert page["summary"]["text"].endswith("· stopped · no decisions, 1 trade · holds 1 SPY.")
    # Selling what it holds is needed; it waits on a clean check against Alpaca.
    sell = next(entry for entry in page["toolbar"] if entry["action_id"] == "prepare_safe_flatten")
    assert (sell["label"], sell["availability"]) == ("Sell 1 SPY", "blocked")
    assert sell["reason"] == "A successful account reconciliation is required before preparing reduction."
    # A holding bot is not finished: Deploy again is offered, never the primary.
    deploy_again = next(entry for entry in page["toolbar"] if entry["action_id"] == "deploy_again")
    assert (deploy_again["availability"], deploy_again["primary"]) == ("available", False)
    assert [(fill["side"], fill["quantity"]) for fill in body["run_fills"]] == [("buy", 1.0)]


async def test_a_bots_page_says_only_what_is_true_while_its_end_waits_for_the_operator(
    api, schedule: _Schedule,  # noqa: F811
) -> None:
    """The bot's end has come, and the Clerk then stops exiting its position by itself (#2854).

    Until then the end's sale is the next pass's, and the page says so. After, the Clerk sells
    nothing at the end, and the page went on saying a sale is queued, that the Clerk sells the
    shares on its next pass, and that the Clerk is carrying the end out.
    """
    app, repo = api
    await _make_held_position(repo, account_id=ACCT, strategy_instance_id=SID, run_id=_run_id(SID), quantity=1.0)
    submit_stop_run(repo, account_id=ACCT, strategy_instance_id=SID, lifecycle_run_id=_run_id(SID))
    registry = get_bot_task_registry()
    registry._running = False  # type: ignore[union-attr]
    end = BotEnd(end_at_ms=_T0)
    registry.end = RecordedEnd.scheduled(end)  # type: ignore[union-attr]
    schedule.ends[SID] = ScheduledEnd(strategy_instance_id=SID, end=end)

    async with _client(app) as client:
        before = (await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel")).json()
        raise_uncertainty(
            repo, strategy_instance_id=SID, reason_code=EXIT_STUCK_REASON_CODE,
            headline="A stuck EXIT exhausted automatic re-drives",
            explanation="1 SPY remains attributed after 3 automatic EXIT re-drives.",
            operator_impact="New exposure stays on hold for this strategy and automatic re-drives stopped.",
            next_step="Run Reconcile now, then execute the presented safe flatten.",
            cause_facts=ExitStuckCause(
                symbol="SPY", attributed_qty=1.0, redrive_count=3, first_observed_at_ms=_T0,
            ).to_mapping(),
            severity="error",
        )
        body = (await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel")).json()

    assert before["bot_page"]["summary"]["text"].endswith("· holds 1 SPY · a sale is queued.")
    assert (before["end"]["waits_for_operator"], before["end"]["notice"]) == (False, None)
    reached, sells = before["end"]["headline"].rsplit(" · ", 1)
    assert sells == "sells"
    summary = body["bot_page"]["summary"]
    assert summary["facts"]["exit_queued"] is False
    assert summary["text"].endswith("· holds 1 SPY."), "the summary says a sale is queued"
    assert (body["end"]["status"], body["end"]["waits_for_operator"]) == ("ending", True)
    assert body["end"]["headline"] == f"{reached} · no order sent", "the headline says the end sells"
    assert body["end"]["explanation"] == (
        "Its end has come and the bot is stopped. "
        "At its end the Clerk sent no order for the position the bot holds."
    )
    assert body["end"]["notice"] == (
        "The Clerk stopped exiting this bot's position by itself, so the end's sale of 1 SPY was not sent. "
        "Sell the position or write it off from the bot's page; the end then completes with no sale."
    )
    change_end = next(entry for entry in body["bot_page"]["toolbar"] if entry["action_id"] == "change_end")
    assert (change_end["availability"], change_end["reason"]) == (
        "blocked",
        "This bot's end has come; it completes once the bot holds no position.",
    )


def _crash_the_run(repo: ClerkSqliteRepository, monkeypatch: pytest.MonkeyPatch) -> None:
    """End the fixture bot's run as a crash, in the registry and the Clerk's lifecycle projection."""
    submit_stop_run(repo, account_id=ACCT, strategy_instance_id=SID, lifecycle_run_id=_run_id(SID))
    registry = get_bot_task_registry()
    registry._running = False  # type: ignore[union-attr]
    crash = BotDutyOutcomeView(
        kind="CRASHED", reason_code="PROCESS_EXITED", recorded_at_ms=_T0 + 60_000, run_id=_run_id(SID)
    )
    # The run record and the Clerk's lifecycle projection both read the run's one terminal receipt.
    registry.duty_outcome = crash  # type: ignore[union-attr]
    read = panel_data_source.read_sqlite_panel_evidence

    async def with_the_crash(*args: object, **kwargs: object):
        evidence = await read(*args, **kwargs)
        return replace(evidence, status=evidence.status.model_copy(update={"duty_outcome": crash}))

    monkeypatch.setattr(panel_data_source, "read_sqlite_panel_evidence", with_the_crash)


async def test_a_crashed_run_needs_attention_and_says_how_it_ended(api, monkeypatch: pytest.MonkeyPatch) -> None:
    app, repo = api
    _crash_the_run(repo, monkeypatch)
    async with _client(app) as client:
        page = (await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel")).json()["bot_page"]

    assert page["status"]["state"] == "needs_attention"
    assert " · crashed · " in page["summary"]["text"]
    assert not any(entry["primary"] for entry in page["toolbar"] if entry["action_id"] == "deploy_again")


async def test_a_crashed_bots_unverified_position_notice_clears_on_the_clerks_own_pass(
    api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2826: the bot page counted only a Reconcile now receipt under 30 s old, so
    the notice came back although the Clerk checks the whole account every 15 s."""
    app, repo = api
    _crash_the_run(repo, monkeypatch)
    facade = get_active_clerk_runtime().clerk
    url = f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel"

    async with _client(app) as client:
        before = (await client.get(url)).json()["health"]["duty_outcome"]["exposure_notices"]
        facade.publish_sweep_reconciliation(AccountReconciliationResult(
            verdict="clean", through_sequence=repo.last_custody_sequence(SID), verdict_at_ms=repo.clock(),
        ))
        after = (await client.get(url)).json()["health"]["duty_outcome"]["exposure_notices"]

    assert [notice["kind"] for notice in before] == ["position_unverified"]
    assert after == []


# ── The Flatten gate accepts the Clerk's own check of the account (#2839) ────

_PANEL = f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel"
_ACTIONS = f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/actions"
_NEEDS_A_CHECK = "A successful account reconciliation is required before preparing reduction."
_UNVERIFIED = ("position_unverified", "Open bot")
_USE_FLATTEN = ("position_unmanaged", "Flatten", "Use Flatten to close this position.")


class _SpyAccount:
    """The Alpaca account the crashed bot's Flatten is checked against as it is sent.

    What it holds is the test's to change; every read it answers is recorded.
    """

    def __init__(self, *, holds: float) -> None:
        self.holds = holds
        self.reads: list[str] = []

    async def list_orders(self, **_kwargs: object) -> list:
        self.reads.append("orders")
        return []

    async def list_positions(self) -> list:
        self.reads.append("positions")
        return [_broker_position_fixture("SPY", quantity=self.holds)] if self.holds else []


@dataclass(frozen=True)
class _CrashedHolding:
    app: FastAPI
    repo: ClerkSqliteRepository
    clock: _TestClock
    facade: SqliteAlpacaClerkFacade
    account: _SpyAccount
    trade: _FakeTradePort


@pytest.fixture()
async def crashed_holding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_CrashedHolding]:
    """A crashed bot still holding 1 SPY, on a Clerk whose clock, account and sends the test controls.

    Inside the regular session, where a Flatten goes out as a market order.
    Nothing has checked the account yet: no Reconcile now, no pass of the Clerk's.
    """
    reset_broker_registry_for_testing()
    reset_idempotency_store_for_testing()
    set_active_clerk_runtime(None)
    set_bot_task_registry(_FakeRegistry(tmp_path, sids=(SID,)))  # type: ignore[arg-type]
    get_broker_registry().register(_FakeBrokerPort())  # type: ignore[arg-type]
    clock = _TestClock(FIXTURE_RTH_MS)
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCT, artifacts_root=tmp_path, clock=clock, lease_ttl_ms=300_000
    )
    repo.register_strategy_instance(
        strategy_instance_id=SID,
        symbol="SPY",
        config_hash="config-1",
        strategy_key="deployment_validation",
        display_name="Deployment Validation",
        config_json=json.dumps({"mode": "trade", "quantity": 1, "carryover_policy": "FORBID"}),
        exit_terms=DEPLOY_EXIT_TERMS,
    )
    seed_historical_run_started(repo, account_id=ACCT, strategy_instance_id=SID, lifecycle_run_id=_run_id(SID))
    account, trade = _SpyAccount(holds=1.0), _FakeTradePort()
    facade = SqliteAlpacaClerkFacade(account_mode="paper", repo=repo, read=account, trade=trade)  # type: ignore[arg-type]
    set_active_clerk_runtime(ActiveClerkRuntime(authority_kind="sqlite", clerk=facade))
    app = FastAPI()
    app.include_router(router)
    try:
        await _make_held_position(repo, account_id=ACCT, strategy_instance_id=SID, run_id=_run_id(SID), quantity=1.0)
        _crash_the_run(repo, monkeypatch)
        yield _CrashedHolding(app=app, repo=repo, clock=clock, facade=facade, account=account, trade=trade)
    finally:
        set_active_clerk_runtime(None)
        set_bot_task_registry(None)
        repo.close()
        reset_broker_registry_for_testing()
        reset_idempotency_store_for_testing()


@dataclass(frozen=True)
class _FlattenOffer:
    """What ONE panel read says about selling the bot's position: the button, the notice and the command."""

    sell_button: tuple[str, str]
    notice: tuple[str, ...]
    command_enabled: bool
    command: dict[str, object]


def _flatten_offer(panel: dict, *, key: str) -> _FlattenOffer:
    sell = next(entry for entry in panel["bot_page"]["toolbar"] if entry["action_id"] == "prepare_safe_flatten")
    execute = next(item for item in panel["actions"] if item["action_id"] == "execute_safe_flatten")
    (notice,) = panel["health"]["duty_outcome"]["exposure_notices"]
    closing = notice["explanation"].rsplit(". ", 1)[-1]
    return _FlattenOffer(
        sell_button=(sell["availability"], sell["reason"]),
        notice=(
            (notice["kind"], notice["action_label"], closing)
            if notice["kind"] == "position_unmanaged"
            else (notice["kind"], notice["action_label"])
        ),
        command_enabled=execute["enabled"],
        command={
            "action_id": "execute_safe_flatten",
            "concurrency_token": execute["concurrency_token"],
            "idempotency_key": key,
        },
    )


async def _clerk_pass(world: _CrashedHolding) -> None:
    """One pass of the Clerk's own 15 s check: it compares the account and publishes; it writes no receipt."""
    result = await world.facade.reconcile_account(trigger="AUTOMATIC")
    assert (result.verdict, result.receipt_id) == ("clean", None)


async def _reconcile_now(world: _CrashedHolding) -> None:
    async with _client(world.app) as client:
        panel = (await client.get(_PANEL)).json()
        action = next(item for item in panel["actions"] if item["action_id"] == "reconcile_now")
        response = await client.post(_ACTIONS, json={
            "action_id": "reconcile_now",
            "concurrency_token": action["concurrency_token"], "idempotency_key": "reconcile-now",
        })
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "check_the_account",
    [pytest.param(_reconcile_now, id="reconcile-now"), pytest.param(_clerk_pass, id="the-clerks-own-pass")],
)
async def test_a_crashed_bots_flatten_is_offered_and_sent_on_either_check_of_the_account(
    crashed_holding: _CrashedHolding, check_the_account: Callable[[_CrashedHolding], Awaitable[None]]
) -> None:
    """#2839: the Clerk checks the whole account every 15 s, yet the page said "Reconcile now, then use Flatten".

    On one panel read the Sell button, the notice and the Flatten command all
    say the position can be sold, and the command sells it -- after reading
    the account as it is sent, whichever check offered it.
    """
    world = crashed_holding
    await check_the_account(world)
    world.account.reads.clear()

    async with _client(world.app) as client:
        offer = _flatten_offer((await client.get(_PANEL)).json(), key="flatten-1")
        committed = await client.post(_ACTIONS, json=offer.command)

    assert offer.sell_button == ("available", "Review a plan to sell what this bot holds, then send it.")
    assert offer.notice == _USE_FLATTEN
    assert offer.command_enabled is True
    assert committed.status_code == 200, committed.text
    assert committed.json()["applied"] is True
    assert world.account.reads == ["orders", "positions"]
    assert [(leg.side, leg.quantity) for leg in world.trade.submitted_legs] == [("sell", 1.0)]


def _nothing_checked_the_account(world: _CrashedHolding) -> None:
    """Neither a Reconcile now nor a pass of the Clerk's."""


def _the_pass_found_the_account_out_of_step(world: _CrashedHolding) -> None:
    world.facade.publish_sweep_reconciliation(AccountReconciliationResult(
        verdict="position_drift", through_sequence=world.repo.last_custody_sequence(),
        verdict_at_ms=world.clock.value,
    ))


def _the_pass_ran_before_the_bots_newest_record(world: _CrashedHolding) -> None:
    world.facade.publish_sweep_reconciliation(AccountReconciliationResult(
        verdict="clean", through_sequence=world.repo.last_custody_sequence(SID) - 1,
        verdict_at_ms=world.clock.value,
    ))


async def _the_pass_is_older_than_the_sweeps_bound(world: _CrashedHolding) -> None:
    await _clerk_pass(world)
    _walk_clock_to(world.repo, world.clock.value + SWEEP_LIVENESS_BOUND_MS + 1)


async def _the_bot_wrote_a_record_after_the_pass(world: _CrashedHolding) -> None:
    await _clerk_pass(world)
    seed_historical_run_started(world.repo, account_id=ACCT, strategy_instance_id=SID, lifecycle_run_id="run-again")
    submit_stop_run(world.repo, account_id=ACCT, strategy_instance_id=SID, lifecycle_run_id="run-again")


@pytest.mark.parametrize(
    "the_account_check",
    [
        pytest.param(_nothing_checked_the_account, id="neither-check"),
        pytest.param(_the_pass_found_the_account_out_of_step, id="the-pass-cannot-vouch"),
        pytest.param(_the_pass_ran_before_the_bots_newest_record, id="the-pass-did-not-see-this-bots-records"),
        pytest.param(_the_pass_is_older_than_the_sweeps_bound, id="the-pass-is-stale"),
        pytest.param(_the_bot_wrote_a_record_after_the_pass, id="a-custody-record-after-the-pass"),
    ],
)
async def test_a_crashed_bots_flatten_is_withheld_when_no_check_of_the_account_stands(
    crashed_holding: _CrashedHolding,
    the_account_check: Callable[[_CrashedHolding], Awaitable[None] | None],
) -> None:
    """With no fresh receipt and no pass that qualifies, the page is what it was before #2839.

    The button, the notice and the command agree from one read, in the same
    words as before; the command is refused, reads nothing and sends nothing.
    """
    world = crashed_holding
    prepared = the_account_check(world)
    if prepared is not None:
        await prepared
    world.account.reads.clear()

    async with _client(world.app) as client:
        offer = _flatten_offer((await client.get(_PANEL)).json(), key="flatten-1")
        committed = await client.post(_ACTIONS, json=offer.command)

    assert offer.sell_button == ("blocked", _NEEDS_A_CHECK)
    assert offer.notice == _UNVERIFIED
    assert offer.command_enabled is False
    assert committed.status_code == 409, committed.text
    assert committed.json()["detail"]["message"] == _NEEDS_A_CHECK
    assert world.account.reads == []
    assert world.trade.submit_calls == []


async def test_a_flatten_presented_under_the_clerks_pass_is_refused_at_commit_once_the_pass_is_stale(
    crashed_holding: _CrashedHolding,
) -> None:
    """The page offered the Flatten under a pass; the sweep then stopped publishing.

    The commit reads the pass again and refuses: it never sells on what the
    presentation saw.
    """
    world = crashed_holding
    await _clerk_pass(world)
    world.account.reads.clear()

    async with _client(world.app) as client:
        offer = _flatten_offer((await client.get(_PANEL)).json(), key="flatten-1")
        _walk_clock_to(world.repo, world.clock.value + SWEEP_LIVENESS_BOUND_MS + 1)
        committed = await client.post(_ACTIONS, json=offer.command)
        after = _flatten_offer((await client.get(_PANEL)).json(), key="flatten-2")

    assert (offer.sell_button[0], offer.notice, offer.command_enabled) == ("available", _USE_FLATTEN, True)
    assert committed.status_code == 409, committed.text
    assert world.account.reads == []
    assert world.trade.submit_calls == []
    assert (after.sell_button, after.notice, after.command_enabled) == (("blocked", _NEEDS_A_CHECK), _UNVERIFIED, False)


async def test_a_flatten_under_the_clerks_pass_is_refused_when_alpaca_no_longer_holds_the_shares(
    crashed_holding: _CrashedHolding,
) -> None:
    """The pass saw 1 SPY; it is gone at Alpaca by the time the Flatten goes out. The send-time read refuses."""
    world = crashed_holding
    await _clerk_pass(world)
    world.account.holds = 0.0
    world.account.reads.clear()

    async with _client(world.app) as client:
        offer = _flatten_offer((await client.get(_PANEL)).json(), key="flatten-1")
        committed = await client.post(_ACTIONS, json=offer.command)

    assert offer.command_enabled is True
    assert committed.status_code == 409, committed.text
    assert "Alpaca holds no SPY, less than the 1 this flatten would sell; nothing was sent." in (
        committed.json()["detail"]["message"]
    )
    assert world.account.reads == ["orders", "positions"]
    assert world.trade.submit_calls == []


async def test_the_clerks_own_routes_prepare_and_send_a_flatten_under_its_pass_by_the_panels_rule(
    crashed_holding: _CrashedHolding,
) -> None:
    """The bot page prepares its sale through the Clerk's check route with the token its panel presented.

    The panel, the Clerk's snapshot and its check and execute routes all read
    the Clerk's pass the same way, so they present one token and one plan --
    and the plan says which check of the account it rests on.
    """
    world = crashed_holding
    await _clerk_pass(world)
    world.account.reads.clear()
    async with _client(world.app) as client:
        panel = (await client.get(_PANEL)).json()
    presented = {item["action_id"]: item["concurrency_token"] for item in panel["actions"]}

    check = await alpaca_clerk_sqlite.check_bot_recovery_action(
        ACCT, SID,
        RecoveryActionCheckRequest(
            action_id="prepare_safe_flatten", concurrency_token=presented["prepare_safe_flatten"]
        ),
    )
    snapshot = await alpaca_clerk_sqlite.get_bot_snapshot(ACCT, SID)
    sent = await alpaca_clerk_sqlite.execute_bot_recovery_action(
        ACCT, SID,
        RecoveryActionExecuteRequest(
            action_id="execute_safe_flatten", concurrency_token=presented["execute_safe_flatten"]
        ),
    )

    plan = check.capability.reduction_plan
    assert plan is not None
    assert (plan.account_check_kind, plan.reconciliation_id) == ("clerk_pass", None)
    assert plan.expires_at_ms == plan.account_check_observed_at_ms + SWEEP_LIVENESS_BOUND_MS
    assert {
        action.action_id: action.concurrency_token
        for action in snapshot.recovery_actions
        if action.action_id in ("prepare_safe_flatten", "execute_safe_flatten")
    } == {action_id: presented[action_id] for action_id in ("prepare_safe_flatten", "execute_safe_flatten")}
    assert sent.applied is True
    assert world.account.reads == ["orders", "positions"]
    assert [(leg.side, leg.quantity) for leg in world.trade.submitted_legs] == [("sell", 1.0)]


async def test_the_clerks_execute_route_refuses_a_flatten_whose_pass_went_stale_after_it_was_presented(
    crashed_holding: _CrashedHolding,
) -> None:
    world = crashed_holding
    await _clerk_pass(world)
    world.account.reads.clear()
    snapshot = await alpaca_clerk_sqlite.get_bot_snapshot(ACCT, SID)
    execute = next(action for action in snapshot.recovery_actions if action.action_id == "execute_safe_flatten")
    assert execute.available

    _walk_clock_to(world.repo, world.clock.value + SWEEP_LIVENESS_BOUND_MS + 1)
    with pytest.raises(HTTPException) as refused:
        await alpaca_clerk_sqlite.execute_bot_recovery_action(
            ACCT, SID,
            RecoveryActionExecuteRequest(
                action_id="execute_safe_flatten", concurrency_token=execute.concurrency_token
            ),
        )

    assert refused.value.status_code == 409
    assert refused.value.detail == {
        "reason": "stale_action_token",
        "message": "The recovery evidence changed. Refresh before confirming the action.",
    }
    assert world.account.reads == []
    assert world.trade.submit_calls == []


async def test_the_accounts_own_flatten_plan_still_waits_for_reconcile_now(
    crashed_holding: _CrashedHolding,
) -> None:
    """An account-scope action never rests on the Clerk's pass, though this bot's own does."""
    world = crashed_holding
    await _clerk_pass(world)

    account = await alpaca_clerk_sqlite.get_account_snapshot(ACCT)
    bot = await alpaca_clerk_sqlite.get_bot_snapshot(ACCT, SID)

    def prepare(actions: list) -> tuple[bool, str | None]:
        action = next(item for item in actions if item.action_id == "prepare_safe_flatten")
        return action.available, action.unavailable_reason

    assert prepare(bot.recovery_actions) == (True, None)
    assert prepare(account.recovery_actions) == (False, _NEEDS_A_CHECK)


def _count_intake_use(facade: SqliteAlpacaClerkFacade, monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count each time the Clerk's intake lock is taken, and each read of its published pass."""
    seen = {"intake_holds": 0, "pass_reads": 0}
    fence = type(facade.intake)
    enter, read_pass = fence.__aenter__, facade.published_pass

    async def counted_enter(lock: object) -> object:
        seen["intake_holds"] += lock is facade.intake
        return await enter(lock)

    async def counted_pass(sid: str) -> object:
        seen["pass_reads"] += 1
        return await read_pass(sid)

    monkeypatch.setattr(fence, "__aenter__", counted_enter)
    monkeypatch.setattr(facade, "published_pass", counted_pass)
    return seen


async def test_a_running_bots_page_reads_no_pass_and_never_takes_the_intake_lock(
    api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A running bot's Flatten never rests on the Clerk's pass, so its page reads none (#2839).

    The read takes the intake lock, and an open page is rebuilt every 5 s: a
    fold in progress would hold every running bot's page up, and a poisoned
    fence would fail it. Once the bot has no ACTIVE run its page does read
    the pass, as its Flatten may rest on it.
    """
    app, repo = api
    seen = _count_intake_use(get_active_clerk_runtime().clerk, monkeypatch)

    async with _client(app) as client:
        running = await client.get(_PANEL)
        snapshot = await alpaca_clerk_sqlite.get_bot_snapshot(ACCT, SID)
        while_running = dict(seen)
        submit_stop_run(repo, account_id=ACCT, strategy_instance_id=SID, lifecycle_run_id=_run_id(SID))
        get_bot_task_registry()._running = False  # type: ignore[union-attr]
        stopped = await client.get(_PANEL)

    assert running.status_code == 200, running.text
    assert running.json()["bot_page"]["status"]["state"] == "running"
    assert [run.state for run in snapshot.runs] == ["ACTIVE"]
    assert while_running == {"intake_holds": 0, "pass_reads": 0}
    assert stopped.status_code == 200, stopped.text
    assert seen == {"intake_holds": 1, "pass_reads": 1}


async def test_a_hold_on_the_bots_own_custody_needs_attention(api) -> None:
    app, repo = api
    raise_uncertainty(
        repo, strategy_instance_id=SID, reason_code="ORDER_OUTCOME_UNKNOWN", headline="Order outcome unknown",
        explanation="The broker did not confirm an order.", operator_impact="New orders wait.",
        next_step="Check against Alpaca.", evidence_refs=("order:o-1",),
    )

    async with _client(app) as client:
        page = (await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel")).json()["bot_page"]

    assert page["status"] == {
        "state": "needs_attention",
        "label": "Needs attention",
        "reason": "This bot's own custody has a hold or an open question.",
    }


async def test_a_bot_whose_fills_are_not_all_proven_needs_attention(api, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _repo = api
    read = panel_data_source.read_sqlite_panel_evidence

    async def with_unproven_fills(*args: object, **kwargs: object):
        evidence = await read(*args, **kwargs)
        economics = evidence.economics
        snapshot = replace(economics.snapshot, execution_coverage="incomplete")
        return replace(evidence, economics=replace(economics, snapshot=snapshot))

    monkeypatch.setattr(panel_data_source, "read_sqlite_panel_evidence", with_unproven_fills)
    async with _client(app) as client:
        page = (await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel")).json()["bot_page"]

    assert (page["status"]["state"], page["status"]["reason"]) == (
        "needs_attention",
        "This bot's fills are not all proven yet.",
    )


async def test_a_runs_fills_are_those_its_orders_name_not_every_later_fill(api, monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex on #2804: a fill is the run's when its order's effect operation names the run, as History counts it."""
    app, repo = api
    await _make_held_position(repo, account_id=ACCT, strategy_instance_id=SID, run_id=_run_id(SID), quantity=1.0)
    registry = get_bot_task_registry()
    # The runner names a run the bot's order was not placed for, started before that fill.
    monkeypatch.setattr(
        registry, "current_run",
        lambda _broker, _sid: SimpleNamespace(run_id="run-other", started_at_ms=_T0, terminal_outcome=None),
    )

    async with _client(app) as client:
        body = (await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel")).json()

    assert body["run_fills"] == []
    assert body["bot_page"]["summary"]["facts"]["trade_count"] == 0


async def test_a_run_the_clerk_admitted_without_a_launch_record_needs_attention(
    api, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex on #2804: a missing launch record for the Clerk's own run is damage, never "Not started yet"."""
    app, _repo = api
    registry = get_bot_task_registry()

    def missing_record(_broker: str, sid: str) -> object:
        raise UnknownBotError(f"Current run '{_run_id(sid)}' has no launch evidence.")

    monkeypatch.setattr(registry, "current_run", missing_record)
    async with _client(app) as client:
        body = (await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel")).json()

    page = body["bot_page"]

    assert page["status"] == {
        "state": "needs_attention",
        "label": "Needs attention",
        "reason": "This bot's latest run record could not be read.",
    }
    assert page["summary"]["facts"]["ending"] == "unreadable"
    assert page["summary"]["facts"]["trade_count"] is None
    assert body["run_fills"] is None
    lines = {line["key"]: line for line in page["health"]["run"]}
    assert lines["decisions"]["value"] == "Not recorded"
    assert lines["orders"]["value"] == "Not recorded"


async def test_a_run_the_clerk_never_admitted_has_not_started(api, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _repo = api
    registry = get_bot_task_registry()
    binding = registry.binding_for_control("alpaca", SID)
    binding.run_id = "run-not-launched"
    monkeypatch.setattr(registry, "binding_for_control", lambda *_: binding)

    def no_record(_broker: str, _sid: str) -> object:
        raise UnknownBotError("Current run 'run-not-launched' has no launch evidence.")

    monkeypatch.setattr(registry, "current_run", no_record)
    async with _client(app) as client:
        page = (await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel")).json()["bot_page"]

    assert page["summary"]["text"] == "Not started yet."


async def test_a_decision_from_before_the_run_is_not_this_runs(api) -> None:
    """Codex on #2804: an earlier run's last decision never dates, or makes late, this one."""
    app, repo = api
    _append_no_action(repo, _T0 - 3_600_000, explanation=None)

    async with _client(app) as client:
        page = (await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel")).json()["bot_page"]

    decisions = next(line for line in page["health"]["run"] if line["key"] == "decisions")
    assert (decisions["state"], decisions["value"], decisions["at_ms"], decisions["note"]) == (
        "ok",
        "no decisions",
        None,
        None,
    )


async def test_a_run_past_the_clerks_decision_retention_counts_at_least_what_it_kept(api) -> None:
    """Codex on #2804: the Clerk keeps a bot's newest 1,000 decisions, so a longer run's count is a floor."""
    app, repo = api
    for minute in range(1, 1_002):
        _append_no_action(repo, _T0 + minute * 60_000, explanation=None)

    async with _client(app) as client:
        facts = (await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/panel")).json()["bot_page"][
            "summary"
        ]["facts"]

    assert (facts["decision_count"], facts["decision_count_is_floor"]) == (1_000, True)
