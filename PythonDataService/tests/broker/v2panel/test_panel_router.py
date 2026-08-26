"""HTTP seam coverage for the active SQLite Broker V2 panel."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    set_active_clerk_runtime,
)
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
from app.broker.alpaca.clerk.sqlite.enter import accept_enter
from app.broker.alpaca.clerk.sqlite.exit import accept_exit
from app.broker.alpaca.clerk.sqlite.repository import (
    ClerkSqliteRepository,
    ExecutionLeaseLost,
)
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.contract.models import BrokerAccountSnapshot, BrokerOrderLeg
from app.broker.contract.registry import (
    get_broker_registry,
    reset_broker_registry_for_testing,
)
from app.routers import broker_v2_gallery, broker_v2_panel
from app.routers.broker_v2_panel import router
from app.schemas.broker_bots import BotStatusView
from app.schemas.broker_v2_panel import BotPanelLiveSnapshot, ChartLiveResponse
from app.schemas.run_admission import RunAdmissionDecision
from app.services import broker_account_snapshot
from app.services.bot_runner import set_bot_task_registry
from app.services.broker_v2_panel.action_execution_service import (
    reset_idempotency_store_for_testing,
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
        receipts_root: Path | None = None,
        *,
        running: bool = True,
        sids: Sequence[str] = (SID,),
    ) -> None:
        self._receipts_root = receipts_root or Path("receipts")
        self._running = running
        self._sids = tuple(sids)
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
        ) as snapshot:
            self.custody_projections.append((sid, snapshot.reconciliation_state))
        raise BotRunnerError("resume admission policy is not modelled in this harness")

    def panel_action_receipt_path(self, sid: str) -> Path:
        return self._receipts_root / f"{sid}-panel-action-receipts.json"

    def status(self, broker: str, sid: str) -> BotStatusView:
        assert broker == "alpaca"
        assert sid in self._sids, sid
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
            duty_outcome=None,
            binding_created_at_ms=_T0,
            last_transition_at_ms=_T0,
        )

    def list_bots(self, broker: str) -> list[BotStatusView]:
        return [self.status(broker, sid) for sid in self._sids]

    def binding_for_control(self, broker: str, sid: str) -> SimpleNamespace:
        self.status(broker, sid)
        return SimpleNamespace(
            strategy_instance_id=sid,
            run_id=_run_id(sid),
            symbol="SPY",
            use_rth=True,
            strategy_key="deployment_validation",
            sealed_program=None,
            mode="trade",
        )

    def dry_run_activity(self, broker: str, sid: str) -> list:
        self.status(broker, sid)
        return []

    def bindings_for_broker(self, broker: str) -> list:
        """No durable dry-run bindings — the catalog is the plain SQLite roster."""
        return []


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
        submit_start_run(
            repo,
            account_id=ACCT,
            strategy_instance_id=sid,
            lifecycle_run_id=_run_id(sid),
        )
    facade = SqliteAlpacaClerkFacade(
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


@pytest.fixture()
def stopped_api(api, tmp_path: Path, fleet_size: int, monkeypatch: pytest.MonkeyPatch):
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
    """Non-vacuity for the per-bot panel surface, attributed per bot.

    A scalar total is the wrong instrument here: the gallery alone fans out
    to every bot on every round, so ``fleet_size * rounds`` is already met
    without a single panel GET doing any custody work. A panel read that
    regressed to returning 200 without running ``preview_resume_admission``
    — the exact custody-projection regression these gates exist to catch —
    would clear a total-only bound on the gallery's output alone.

    Counting per sid closes that: every bot must be projected at least
    ``_PROJECTIONS_PER_BOT_PER_ROUND * rounds`` times, so losing either
    contributing surface halves that bot's count and fails.
    """
    counts = Counter(sid for sid, _state in registry.custody_projections)
    expected = _PROJECTIONS_PER_BOT_PER_ROUND * rounds
    short = {
        sid: counts[sid] for sid in _fleet_sids(fleet_size) if counts[sid] < expected
    }
    assert not short, f"bots projected fewer than {expected} times: {short}"


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
    assert len(response.json()["stations"]) == 6
    assert "clear_hold" not in response.json()["supported_action_ids"]
    assert "record_inventory_baseline" not in response.json()["supported_action_ids"]


async def test_catalog_scoped_returns_sqlite_roster(api) -> None:
    app, _repo = api
    async with _client(app) as client:
        response = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/catalog")

    assert response.status_code == 200
    rows = response.json()
    assert [row["strategy_instance_id"] for row in rows] == [SID]
    assert rows[0]["strategy_key"] == "deployment_validation"
    assert rows[0]["desired_state"] == "RUNNING"


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
    entered = accept_enter(
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
            as_of_ms=_T0,
        ),
    )

    class _Hub:
        async def snapshot(self) -> BotPanelLiveSnapshot:
            return snapshot

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
            "revision": panel.json()["revision"],
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


async def test_live_chart_accepts_five_second_resolution(
    api,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _repo = api
    observed: list[str] = []

    async def live_chart(
        broker: str,
        account_id: str,
        sid: str,
        *,
        resolution: str,
    ) -> dict[str, object]:
        observed.append(resolution)
        return {
            "strategy_instance_id": sid,
            "symbol": "SPY",
            "trading_date_open_ms": _T0,
            "trading_date_close_ms": _T0 + 60_000,
            "resolution": resolution,
            "bars": [],
            "fill_markers": [],
            "overlay_notices": [],
            "as_of_ms": _T0,
        }

    monkeypatch.setattr("app.routers.broker_v2_panel.ds.get_live_chart", live_chart)
    async with _client(app) as client:
        response = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/chart/live",
            params={"resolution": "5s"},
        )

    assert response.status_code == 200
    assert response.json()["resolution"] == "5s"
    assert observed == ["5s"]


async def test_chart_contract_rejects_unknown_resolution_and_timeframe(api) -> None:
    app, _repo = api
    async with _client(app) as client:
        live = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/chart/live",
            params={"resolution": "15s"},
        )
        history = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/{SID}/chart/history",
            params={"timeframe": "5m"},
        )

    assert live.status_code == 422
    assert history.status_code == 422


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
            "revision": panel.json()["revision"],
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
