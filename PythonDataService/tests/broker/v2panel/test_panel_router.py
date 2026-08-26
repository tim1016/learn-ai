"""HTTP seam coverage for the active SQLite Broker V2 panel."""

from __future__ import annotations

import asyncio
import json
import logging
import statistics
import time
from collections.abc import Sequence
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
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
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
from app.services.bot_runner import set_bot_task_registry
from app.services.broker_v2_panel.action_execution_service import (
    reset_idempotency_store_for_testing,
)
from tests.broker.v2panel.fixtures import ACCT, SID

logger = logging.getLogger(__name__)

_T0 = 1_700_000_000_000


def _run_id(sid: str) -> str:
    return f"run-{sid}"


def _fleet_sids(size: int) -> tuple[str, ...]:
    """``SID`` first (so single-bot tests are the ``size == 1`` case),
    then ``size - 1`` siblings. Kept short: a strategy instance id over 25
    characters is rejected downstream by order-identity namespacing."""
    return (SID, *(f"bot-{index:02d}" for index in range(1, size)))


def _account_snapshot() -> BrokerAccountSnapshot:
    """The real contract model, not a duck-typed stub.

    The deploy view reads mode/status/blocked flags off this, so a stub with
    only ``account_id`` would make the deploy read unreachable — and a read
    that 500s proves nothing about its call budget.
    """
    return BrokerAccountSnapshot(
        broker="alpaca",
        account_id=ACCT,
        account_mode="paper",
        account_status="ACTIVE",
        currency="USD",
        cash=100_000.0,
        equity=100_000.0,
        buying_power=200_000.0,
        portfolio_value=100_000.0,
        long_market_value=0.0,
        short_market_value=0.0,
        pattern_day_trader=False,
        trading_blocked=False,
        account_blocked=False,
        created_at_ms=_T0,
        observed_at_ms=_T0,
    )


class _FakeBrokerPort:
    broker_id = "alpaca"

    def __init__(self) -> None:
        # #1776 WP2: reads are pure, so this counter is the acceptance gate.
        self.calls = 0
        self.methods: list[str] = []

    async def get_account(self) -> BrokerAccountSnapshot:
        self.calls += 1
        self.methods.append('get_account')
        return _account_snapshot()

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
        self.projected_states: list[str] = []

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
            self.projected_states.append(snapshot.reconciliation_state)
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
    """
    monkeypatch.setattr(broker_v2_gallery, "_HUB_CACHE", {})
    monkeypatch.setattr(broker_v2_gallery, "_GALLERY_IO_CACHE_TTL_MS", 0)
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


_READ_ROUNDS = 10


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
    assert deploy.json()["readiness_checks"], "deploy view published no readiness checks"


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

        for _ in range(_READ_ROUNDS):
            for path in paths:
                assert (await client.get(path)).status_code == 200, path

    # Non-vacuity: the stopped-bot custody path really ran, at least once per
    # bot per round — otherwise a gate of "zero broker calls" would pass on a
    # read that never executed.
    assert len(registry.projected_states) >= _READ_ROUNDS * fleet_size, len(
        registry.projected_states
    )
    assert port.calls == 0, f"a read reached the broker port: {port.methods}"


@pytest.mark.parametrize("fleet_size", [1, 50], indirect=True)
async def test_repeated_reads_never_advance_the_control_revision(
    stopped_api, fleet_size: int
) -> None:
    """Revision-drift gate (#1776 WP2): no read appends to the custody ledger.

    Reads that append also move the revision panel actions fence against,
    which is the S16 observer effect.
    """
    app, repo, registry = stopped_api
    before = repo.control_meta_snapshot().control_revision
    paths = _read_paths(fleet_size)

    async with _client(app) as client:
        await _assert_every_read_surface_projected_the_whole_fleet(client, fleet_size)
        for _ in range(_READ_ROUNDS):
            for path in paths:
                assert (await client.get(path)).status_code == 200, path

    assert len(registry.projected_states) >= _READ_ROUNDS * fleet_size, len(
        registry.projected_states
    )
    assert repo.control_meta_snapshot().control_revision - before == 0


@pytest.mark.parametrize("fleet_size", [50], indirect=True)
async def test_panel_read_latency_p95_is_recorded_as_a_diagnostic(
    stopped_api,
    fleet_size: int,
    record_property,
) -> None:
    """NON-GATING (#1776 WP2): p95 panel-read latency, for trend tracking only.

    Wall-clock is deliberately not an acceptance gate — CI hardware variance
    would make it flaky, and the real invariants (zero broker calls, zero
    revision drift) are already asserted above. This records the number so a
    regression in read cost is visible across runs; it must never fail on
    timing, so nothing here asserts a duration.

    Read the value from the junit XML property ``panel_read_p95_ms`` (with
    ``-o junit_family=legacy``, which xunit2 needs for per-test properties),
    or run with ``--log-cli-level=INFO`` to see it on the console.
    """
    app, _repo, _registry = stopped_api
    sids = _fleet_sids(fleet_size)
    durations_ms: list[float] = []

    async with _client(app) as client:
        # Warm the process-wide caches (account identity, validation manifest)
        # so the sample measures steady-state read cost, not first-call setup.
        await client.get(f"{_ACCOUNT_ROOT}/bots/{SID}/panel")
        for sid in sids:
            started_ns = time.perf_counter_ns()
            response = await client.get(f"{_ACCOUNT_ROOT}/bots/{sid}/panel")
            durations_ms.append((time.perf_counter_ns() - started_ns) / 1e6)
            assert response.status_code == 200, response.text

    assert len(durations_ms) == len(sids)
    p95_ms = statistics.quantiles(durations_ms, n=20, method="inclusive")[-1]
    record_property("panel_read_fleet_size", len(sids))
    record_property("panel_read_p95_ms", round(p95_ms, 3))
    record_property("panel_read_median_ms", round(statistics.median(durations_ms), 3))
    logger.info(
        "panel read latency diagnostic (non-gating)",
        extra={
            "fleet_size": len(sids),
            "p95_ms": round(p95_ms, 3),
            "median_ms": round(statistics.median(durations_ms), 3),
        },
    )


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
