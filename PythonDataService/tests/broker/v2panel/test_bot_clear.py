"""Clearing finished bots from Home (owner decision 2026-09-28, #2567).

Each leg is the unchanged per-bot ``archive`` (ADR 0052) run through the
shared batch executor, so what these tests pin is what clearing adds: the
request names bots and never an action, each leg runs under its own panel's
presented token and a derived identity, a leg the guard refuses carries the
guard's own reason while its siblings clear, and a resend replays.

The guard itself -- re-proved against fresh custody under the bot's lock --
is pinned where it lives (``test_registry_lifecycle``'s archive cases and
``test_archive_eligibility``); the catalog leaving cleared bots out is pinned
in ``test_sqlite_roster_source``. The batch's one reconciliation pass is
pinned at the owner's scale against the real stack at the end of this module,
and the cut that pass hands each leg in ``test_runtime``.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from pydantic import ValidationError

from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    close_synthetic_clerk_runtimes,
    get_clerk_runtime,
    set_active_clerk_runtime,
)
from app.broker.alpaca.clerk.models import ReconciliationCut
from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
from app.broker.alpaca.clerk.sqlite.reconciliation_sweep import ReconciliationSweep
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.contract.errors import BrokerError, BrokerRateLimited
from app.broker.contract.models import BrokerAccountSnapshot, BrokerOrder, BrokerPosition
from app.broker.contract.registry import get_broker_registry, reset_broker_registry_for_testing
from app.broker.fleet.internal_http import BOTS_CLEAR_READ_TIMEOUT_S
from app.engine.live.bot_lifecycle_state import (
    BotLifecyclePhase,
    BotLifecycleStateRepo,
    stable_bot_lifecycle_state_path,
)
from app.routers.broker_v2_panel import router
from app.schemas.broker_v2_panel import BotClearRequest, PanelActionRequest, PanelActionResult
from app.schemas.deployment_budget import DeployBudgetConsent
from app.schemas.market_liveness import MarketStatusSnapshot, MarketStatusSource, TopOfBookQuote
from app.services import market_liveness
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.bot_runner import BotTaskRegistry, set_bot_task_registry
from app.services.broker_v2_panel import bot_clear, panel_data_source, panel_scope
from app.services.broker_v2_panel.action_execution_service import (
    ActionNotAvailableError,
    reset_idempotency_store_for_testing,
)
from app.utils.timestamps import now_ms_utc
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS
from tests.broker.alpaca.clerk.sqlite.conftest import _broker_position_fixture, _make_held_position
from tests.broker.v2panel.conftest import account_snapshot
from tests.broker.v2panel.fixtures import ACCT

_FINISHED = "spy-done-1"
_DRY_RUN = "dry-done-1"
_HOLDING = "qqq-holding-1"
_RACED = "tsla-raced-1"
_KNOWN = (_FINISHED, _DRY_RUN, _HOLDING, _RACED)

_STRAND_HEADLINE = "This bot still holds shares or has a working order."
_STRAND_WHY = "Flatten it and let its working orders finish, then clear it."


async def _accept_account(broker: str, account_id: str) -> str:
    return account_id


def _panel(sid: str, *, enabled: bool) -> SimpleNamespace:
    """A bot page's presentation: its archive action and that action's token."""
    return SimpleNamespace(actions=[
        SimpleNamespace(action_id="stop_bot_decisions", revision=3, concurrency_token=f"stop-{sid}", enabled=False),
        SimpleNamespace(action_id="archive", revision=5, concurrency_token=f"token-{sid}", enabled=enabled),
    ])


class _Lane:
    """The per-bot pipeline, as the clear orchestration sees it.

    ``holding`` bots present a disabled archive and refuse it with the guard's
    reason; ``raced`` bots present it armed but a fill lands before the click,
    so the commit-time guard refuses. ``applied`` records every idempotency
    key the pipeline completed, so a resend replays instead of reapplying.

    A bot this lane does not know is read through the real panel read against
    a real, empty bot runner, so the lookup fails exactly as production's does
    -- with the runner's own error type, not a stand-in for it.
    """

    def __init__(
        self,
        real_get_panel: Callable[[str, str, str], Awaitable[object]],
        *,
        holding: tuple[str, ...] = (),
        raced: tuple[str, ...] = (),
    ) -> None:
        self.real_get_panel = real_get_panel
        self.holding = holding
        self.raced = raced
        self.applied: set[str] = set()
        self.calls: list[tuple[str, str, str, str]] = []

    async def get_panel(self, broker: str, account_id: str, sid: str) -> object:
        if sid not in _KNOWN:
            return await self.real_get_panel(broker, account_id, sid)
        return _panel(sid, enabled=sid not in self.holding)

    async def run_action(
        self,
        broker: str,
        account_id: str,
        sid: str,
        request: PanelActionRequest,
        *,
        operator_identity: str,
        reconciled: ReconciliationCut | None = None,
    ) -> PanelActionResult:
        self.calls.append((sid, request.action_id, request.idempotency_key, request.concurrency_token))
        replay = request.idempotency_key in self.applied
        if not replay and (sid in self.holding or sid in self.raced):
            raise ActionNotAvailableError(
                _STRAND_HEADLINE, detail=_STRAND_WHY, reason_code="ARCHIVE_WOULD_STRAND_CUSTODY"
            )
        self.applied.add(request.idempotency_key)
        return PanelActionResult(
            action_id="archive",
            receipt_id=request.idempotency_key,
            recorded_at_ms=1,
            applied=not replay,
            revision=5,
            concurrency_token=request.concurrency_token,
            message="Bot archived and taken off the roster.",
        )


def _empty_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A real bot runner with no bots bound, behind a route account that resolves."""
    runner = BotTaskRegistry(tmp_path / "runner", feed_resolver=lambda: None, boot_recovery_required=False)
    monkeypatch.setattr(panel_data_source, "get_bot_task_registry", lambda: runner)
    monkeypatch.setattr(panel_data_source, "validate_account", _accept_account)


@pytest.fixture()
def lane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Lane:
    _empty_runner(tmp_path, monkeypatch)
    fake = _Lane(panel_data_source.get_panel, holding=(_HOLDING,), raced=(_RACED,))
    monkeypatch.setattr(panel_data_source, "get_panel", fake.get_panel)
    monkeypatch.setattr(panel_data_source, "run_action", fake.run_action)
    monkeypatch.setattr(bot_clear, "validate_account", _accept_account)
    return fake


def _request(*sids: str, key: str = "clear-1") -> BotClearRequest:
    return BotClearRequest(idempotency_key=key, strategy_instance_ids=list(sids))


# ── request contract ─────────────────────────────────────────────────────────


def test_a_clear_request_cannot_name_the_action_it_runs() -> None:
    """The endpoint clears, and nothing else: no field can steer it elsewhere."""
    with pytest.raises(ValidationError):
        BotClearRequest.model_validate(
            {"idempotency_key": "k", "strategy_instance_ids": [_FINISHED], "action_id": "execute_safe_flatten"}
        )


def test_a_bot_is_cleared_once_per_request() -> None:
    with pytest.raises(ValidationError, match="once per request"):
        _request(_FINISHED, _FINISHED)


def test_the_derived_leg_identity_budget_is_enforced() -> None:
    with pytest.raises(ValidationError, match="identity budget"):
        BotClearRequest(idempotency_key="k" * 64, strategy_instance_ids=["s" * 96])


@pytest.mark.parametrize("malformed", ["evil id", "../escape", ".hidden", "spy/done"])
def test_a_malformed_bot_id_is_refused_at_the_request_boundary(malformed: str) -> None:
    """Every id must be a strategy instance id; a malformed one never reaches a leg."""
    with pytest.raises(ValidationError, match="strategy_instance_ids"):
        _request(_FINISHED, malformed)


# ── the legs ─────────────────────────────────────────────────────────────────


async def test_finished_bots_clear_a_dry_run_included(lane: _Lane) -> None:
    """Each leg is the per-bot archive under its own panel's token and the
    derived identity ``{key}:{sid}``, in request order."""
    result = await bot_clear.clear_bots("alpaca", ACCT, _request(_FINISHED, _DRY_RUN), operator_identity="owner")

    assert lane.calls == [
        (sid, "archive", f"clear-1:{sid}", f"token-{sid}") for sid in (_FINISHED, _DRY_RUN)
    ]
    assert [(leg.strategy_instance_id, leg.outcome) for leg in result.legs] == [
        (_FINISHED, "applied"), (_DRY_RUN, "applied"),
    ]
    assert (result.applied_count, result.refused_count, result.receipt_id) == (2, 0, "clear-1")


async def test_a_holding_bot_is_refused_with_its_own_reason_while_the_others_clear(lane: _Lane) -> None:
    result = await bot_clear.clear_bots(
        "alpaca", ACCT, _request(_FINISHED, _HOLDING, _DRY_RUN), operator_identity="owner"
    )

    assert [(leg.strategy_instance_id, leg.outcome) for leg in result.legs] == [
        (_FINISHED, "applied"), (_HOLDING, "refused"), (_DRY_RUN, "applied"),
    ]
    refused = result.legs[1].error
    assert refused is not None
    assert (refused.outcome, refused.reason_code) == ("conflict", "ARCHIVE_WOULD_STRAND_CUSTODY")
    assert (refused.message, refused.why) == (_STRAND_HEADLINE, _STRAND_WHY)
    assert (result.applied_count, result.refused_count) == (2, 1)


async def test_a_fill_between_the_owners_look_and_the_click_is_refused_at_commit(lane: _Lane) -> None:
    """The panel presented the leg armed, but the commit-time guard -- run
    against fresh custody under the bot's lock -- refuses: the bot is not
    cleared, and its refusal says why."""
    result = await bot_clear.clear_bots("alpaca", ACCT, _request(_RACED, _FINISHED), operator_identity="owner")

    assert [(leg.strategy_instance_id, leg.outcome) for leg in result.legs] == [
        (_RACED, "refused"), (_FINISHED, "applied"),
    ]
    assert result.legs[0].error is not None
    assert result.legs[0].error.reason_code == "ARCHIVE_WOULD_STRAND_CUSTODY"
    assert f"clear-1:{_RACED}" not in lane.applied


async def test_a_resend_replays_the_cleared_legs_and_retries_only_the_refused(lane: _Lane) -> None:
    first = await bot_clear.clear_bots("alpaca", ACCT, _request(_FINISHED, _HOLDING), operator_identity="owner")
    lane.holding = ()  # flattened in between: the refused leg may now clear
    again = await bot_clear.clear_bots("alpaca", ACCT, _request(_FINISHED, _HOLDING), operator_identity="owner")

    assert [leg.outcome for leg in first.legs] == ["applied", "refused"]
    assert [leg.outcome for leg in again.legs] == ["replayed", "applied"]
    assert (again.replayed_count, again.applied_count) == (1, 1)


async def test_an_unknown_bot_is_refused_and_never_aborts_its_siblings(lane: _Lane) -> None:
    """The runner has no binding for the id and says so with its own error;
    that leg is refused with the runner's words and its siblings still clear."""
    result = await bot_clear.clear_bots(
        "alpaca", ACCT, _request(_FINISHED, "not-a-bot", _DRY_RUN), operator_identity="owner"
    )

    assert [(leg.strategy_instance_id, leg.outcome) for leg in result.legs] == [
        (_FINISHED, "applied"), ("not-a-bot", "refused"), (_DRY_RUN, "applied"),
    ]
    unknown = result.legs[1].error
    assert unknown is not None
    assert (unknown.outcome, unknown.reason_code) == ("conflict", "CLEAR_BOT_NOT_FOUND")
    assert unknown.message == "No bot 'not-a-bot' is bound to broker 'alpaca'."
    assert [call[0] for call in lane.calls] == [_FINISHED, _DRY_RUN]
    assert (result.applied_count, result.refused_count) == (2, 1)


async def test_the_clear_route_answers_every_leg(lane: _Lane) -> None:
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/clear",
            json={"idempotency_key": "clear-route", "strategy_instance_ids": [_FINISHED, _HOLDING]},
        )
        malformed = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/clear",
            json={"idempotency_key": "clear-route", "strategy_instance_ids": []},
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert [(leg["strategy_instance_id"], leg["outcome"]) for leg in body["legs"]] == [
        (_FINISHED, "applied"), (_HOLDING, "refused"),
    ]
    assert body["legs"][1]["error"]["reason_code"] == "ARCHIVE_WOULD_STRAND_CUSTODY"
    assert malformed.status_code == 422


async def test_a_malformed_bot_id_is_a_422_and_no_leg_runs(lane: _Lane) -> None:
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/clear",
            json={"idempotency_key": "clear-route", "strategy_instance_ids": [_FINISHED, "evil id"]},
        )

    assert response.status_code == 422, response.text
    assert lane.calls == []


@pytest.mark.parametrize("sid", ["not-a-bot", "evil id"])
async def test_the_bot_page_of_an_unknown_bot_is_not_found(
    sid: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bot page's panel read shares the clear leg's lookup: an id the runner
    has no binding for -- or cannot even hold one for -- is a 404 in the
    runner's own words, never an internal error."""
    _empty_runner(tmp_path, monkeypatch)
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{sid}/panel")

    assert response.status_code == 404, response.text
    assert response.json()["detail"]["message"]


async def test_the_commit_time_refusal_is_a_typed_refusal_not_an_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``bot_runner.archive`` refuses under the bot's lock before any write
    (ADR 0052 §3). The per-bot performer reports that as the guard's typed
    refusal -- nothing applied, key released for a retry -- instead of the
    "outcome unknown" every untyped performer error becomes."""
    from app.services.bot_runner_errors import BotRunnerError

    class _Registry:
        async def archive(
            self, broker: str, sid: str, *, updated_by: str, reason: str | None, reconciled: ReconciliationCut | None,
        ) -> None:
            raise BotRunnerError(_STRAND_HEADLINE, detail=_STRAND_WHY, reason_code="ARCHIVE_WOULD_STRAND_CUSTODY")

    monkeypatch.setattr(panel_data_source, "get_bot_task_registry", lambda: _Registry())
    archive = panel_data_source._action_performers("alpaca", _RACED)["archive"]

    with pytest.raises(ActionNotAvailableError) as refused:
        await archive("owner", "Cleared from Home")

    assert (str(refused.value), refused.value.detail) == (_STRAND_HEADLINE, _STRAND_WHY)
    assert refused.value.reason_code == "ARCHIVE_WOULD_STRAND_CUSTODY"


# ── the owner's scale, against the real stack ────────────────────────────────

#: One REST round trip to Alpaca, as a lane sees it.
_ALPACA_ROUND_TRIP_S = 0.1
#: Alpaca's REST allowance per account (``rest_rate_limit_per_min``).
_ALPACA_REQUESTS_PER_MINUTE = 200


class _Alpaca:
    """Alpaca's REST port as a clear feels it: a round trip per request, 200 a minute.

    Past the allowance it answers 429, as Alpaca does -- which reconciliation
    takes as unreadable broker truth, putting the account on hold.
    """

    broker_id = "alpaca"

    def __init__(self) -> None:
        self.positions: list[BrokerPosition] = []
        self.requests = 0
        self.unreachable = False
        self._window: deque[float] = deque()

    async def _request(self) -> None:
        if self.unreachable:
            self.requests += 1
            raise BrokerError("Alpaca could not be reached.", broker="alpaca")
        now = time.monotonic()
        while self._window and now - self._window[0] >= 60:
            self._window.popleft()
        if len(self._window) >= _ALPACA_REQUESTS_PER_MINUTE:
            raise BrokerRateLimited("Alpaca answered 429: too many requests.", broker="alpaca")
        self._window.append(now)
        self.requests += 1
        await asyncio.sleep(_ALPACA_ROUND_TRIP_S)

    async def get_account(self) -> BrokerAccountSnapshot:
        await self._request()
        return account_snapshot()

    async def list_orders(self, **_kwargs: object) -> list[BrokerOrder]:
        await self._request()
        return []

    async def list_positions(self) -> list[BrokerPosition]:
        await self._request()
        return list(self.positions)

    async def get_order_by_client_order_id(self, _client_order_id: str) -> BrokerOrder | None:
        await self._request()
        return None

    async def submit(self, *_args: object, **_kwargs: object) -> BrokerOrder:
        raise AssertionError("clearing never submits an order")

    async def cancel(self, _order_id: str) -> None:
        raise AssertionError("clearing never cancels an order")


@dataclass(frozen=True)
class _Account:
    """One paper account's real Clerk ledger, bot runner and panel, behind a timed Alpaca."""

    repo: ClerkSqliteRepository
    alpaca: _Alpaca
    facade: SqliteAlpacaClerkFacade
    runner: BotTaskRegistry
    runner_root: Path

    def _deployed(self, sid: str) -> None:
        self.repo.register_strategy_instance(
            exit_terms=DEPLOY_EXIT_TERMS,
            strategy_instance_id=sid,
            symbol="SPY",
            config_hash="config-1",
            strategy_key="deployment_validation",
            display_name="Deployment Validation",
            config_json=json.dumps({"mode": "trade", "quantity": 1, "carryover_policy": "FORBID"}),
        )
        submit_start_run(self.repo, account_id=ACCT, strategy_instance_id=sid, lifecycle_run_id=f"run-{sid}")
        self.runner._bindings.record_launch(
            BrokerBotBinding(
                strategy_instance_id=sid,
                strategy_key="deployment_validation",
                broker="alpaca",
                symbol="SPY",
                mode="trade",
                quantity=1,
                action_plan=alpaca_v1_action_plan("SPY"),
                run_id=f"run-{sid}",
                created_at_ms=1,
                sealed_account_id=ACCT,
                exit_terms=DEPLOY_EXIT_TERMS,
            ),
            launch_reason="deploy",
        )

    def _stopped(self, sid: str) -> None:
        submit_stop_run(
            self.repo, account_id=ACCT, strategy_instance_id=sid, lifecycle_run_id=f"run-{sid}", operator_reason="done"
        )

    def finished(self, *sids: str) -> None:
        """Bots that ran and stopped flat, with nothing working."""
        for sid in sids:
            self._deployed(sid)
            self._stopped(sid)

    def dead_before_its_run_settled(self, sid: str) -> None:
        """A bot whose process died before its run settled (ADR 0052 §1's window).

        The Clerk has ended the run -- the sweep ends a run whose runner is
        gone (#2369) -- but the task died before the runner's own duty record
        committed, so that record still says ON_DUTY for a run nobody holds.
        """
        self.finished(sid)
        BotLifecycleStateRepo(stable_bot_lifecycle_state_path(self.runner_root, sid)).set_phase(
            BotLifecyclePhase.ON_DUTY,
            now_ms=1,
            updated_by="runner",
            active_run_id=f"run-{sid}",
        )

    async def holding(self, sid: str) -> None:
        """A bot stopped while it still holds 10 SPY, which Alpaca reports."""
        self._deployed(sid)
        await _make_held_position(self.repo, account_id=ACCT, strategy_instance_id=sid, run_id=f"run-{sid}")
        self._stopped(sid)
        self.alpaca.positions = [_broker_position_fixture("SPY", quantity=10.0)]

    async def stopped_dry_run(self, sid: str) -> None:
        """A Dry Run that ran and stopped flat in its own simulated account, which is closed again."""
        binding = BrokerBotBinding(
            strategy_instance_id=sid,
            strategy_key="deployment_validation",
            broker="alpaca",
            symbol="SPY",
            mode="dry_run",
            quantity=1,
            action_plan=alpaca_v1_action_plan("SPY"),
            run_id=f"run-{sid}",
            created_at_ms=1,
            sealed_account_id=f"sim:{sid}",
            exit_terms=DEPLOY_EXIT_TERMS,
            budget_consent=DeployBudgetConsent(
                committed_cents=100_000, risk_revision=0, actor="owner", request_fingerprint="reviewed",
                world="synthetic",
            ),
        )
        self.runner._bindings.record_launch(binding, launch_reason="deploy")
        # The Deploy priced its budget from a fresh IBKR book.
        now_ms = now_ms_utc()
        market_liveness.get_market_liveness_store().apply_status_snapshot(MarketStatusSnapshot(
            source=MarketStatusSource.IBKR, connected=True, observed_at_ms=now_ms, connection_changed_at_ms=now_ms,
            symbol_statuses=(), quotes=(
                TopOfBookQuote(symbol="SPY", bid=600.0, ask=600.0, source="ibkr.market_data.status", observed_at_ms=now_ms),
            ),
        ), now_ms=now_ms)
        simulator = self.runner._authority_for(binding)
        await simulator.ensure_recoverable()
        runtime = get_clerk_runtime(f"sim:{sid}")
        assert runtime is not None and runtime.clerk is not None
        await runtime.clerk.register_strategy_run(binding)
        await runtime.clerk.stop_strategy_run(strategy_instance_id=sid, run_id=binding.run_id, reason="done")
        await simulator.release_after_run_end()
        assert get_clerk_runtime(f"sim:{sid}") is None

    async def requests_per_pass(self) -> int:
        """What one sweep pass costs at Alpaca -- and the verdict it leaves standing."""
        before = self.alpaca.requests
        await self.facade.reconcile_account(trigger="AUTOMATIC")
        return self.alpaca.requests - before


@pytest.fixture()
async def account(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Account]:
    reset_broker_registry_for_testing()
    reset_idempotency_store_for_testing()
    market_liveness.reset_market_liveness_store_for_testing()
    alpaca = _Alpaca()
    get_broker_registry().register(alpaca)  # type: ignore[arg-type]

    async def _cached_account(_broker: str) -> BrokerAccountSnapshot:
        return account_snapshot()

    # The route's account is the cached snapshot's, never a fresh Alpaca call.
    monkeypatch.setattr(panel_scope, "resolve_account_snapshot", _cached_account)
    repo = ClerkSqliteRepository.initialize(account_id=ACCT, artifacts_root=tmp_path / "clerk")
    facade = SqliteAlpacaClerkFacade(account_mode="paper", repo=repo, read=alpaca, trade=alpaca)  # type: ignore[arg-type]
    set_active_clerk_runtime(ActiveClerkRuntime(authority_kind="sqlite", clerk=facade, _sqlite_repository=repo))
    runner_root = tmp_path / "runner"
    runner = BotTaskRegistry(runner_root, feed_resolver=lambda: None, boot_recovery_required=False)
    set_bot_task_registry(runner)
    try:
        yield _Account(repo=repo, alpaca=alpaca, facade=facade, runner=runner, runner_root=runner_root)
    finally:
        await close_synthetic_clerk_runtimes()
        set_active_clerk_runtime(None)
        set_bot_task_registry(None)
        repo.close()
        reset_broker_registry_for_testing()
        reset_idempotency_store_for_testing()
        market_liveness.reset_market_liveness_store_for_testing()


async def test_clearing_150_finished_bots_reconciles_once_within_the_bound(account: _Account) -> None:
    """The owner's case: a past profile ended with 142 stopped, flat bots (ADR 0052).

    Per leg, each bot's own account pass made four Alpaca requests: 600 for
    150 bots against Alpaca's 200 a minute, so a third of the way in Alpaca
    answered 429, the pass went stale, the account went on hold and every
    later bot was refused. One pass for the batch makes the clear four
    requests at any size, and it finishes well inside the request's bound.
    """
    sids = [f"spy-done-{index:03d}" for index in range(150)]
    account.finished(*sids)
    per_pass = await account.requests_per_pass()
    before = account.alpaca.requests

    started = time.monotonic()
    result = await bot_clear.clear_bots("alpaca", ACCT, _request(*sids, key="clear-all"), operator_identity="owner")
    elapsed_s = time.monotonic() - started

    assert result.applied_count == 150, [leg.error for leg in result.legs if leg.error][:2]
    assert account.alpaca.requests - before == per_pass
    assert elapsed_s < BOTS_CLEAR_READ_TIMEOUT_S


async def test_a_bot_that_moved_since_the_batch_pass_is_reconciled_for_itself(
    account: _Account, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The batch's pass vouches only for bots with no custody activity since it
    began; one that ran again meanwhile is re-proved by a pass of its own."""
    account.finished("spy-done-a", "spy-done-b", "spy-done-c")
    per_pass = await account.requests_per_pass()
    batch_pass = account.facade.reconcile_through

    async def bot_b_runs_again_after_the_pass() -> ReconciliationCut:
        cut = await batch_pass()
        submit_start_run(account.repo, account_id=ACCT, strategy_instance_id="spy-done-b", lifecycle_run_id="again")
        submit_stop_run(account.repo, account_id=ACCT, strategy_instance_id="spy-done-b", lifecycle_run_id="again")
        return cut

    monkeypatch.setattr(account.facade, "reconcile_through", bot_b_runs_again_after_the_pass)
    before = account.alpaca.requests

    result = await bot_clear.clear_bots(
        "alpaca", ACCT, _request("spy-done-a", "spy-done-b", "spy-done-c"), operator_identity="owner"
    )

    assert [leg.outcome for leg in result.legs] == ["applied", "applied", "applied"]
    assert account.alpaca.requests - before == 2 * per_pass


async def test_a_holding_bot_is_refused_from_the_batch_pass_while_its_siblings_clear(account: _Account) -> None:
    account.finished("spy-done-a", "spy-done-c")
    await account.holding("spy-holding-b")
    per_pass = await account.requests_per_pass()
    before = account.alpaca.requests

    result = await bot_clear.clear_bots(
        "alpaca", ACCT, _request("spy-done-a", "spy-holding-b", "spy-done-c"), operator_identity="owner"
    )

    assert [(leg.strategy_instance_id, leg.outcome) for leg in result.legs] == [
        ("spy-done-a", "applied"), ("spy-holding-b", "refused"), ("spy-done-c", "applied"),
    ]
    holding = result.legs[1].error
    assert holding is not None
    assert (holding.reason_code, holding.message) == (
        "ARCHIVE_WOULD_STRAND_CUSTODY", "This bot still holds shares or has a working order.",
    )
    assert account.alpaca.requests - before == per_pass


async def test_an_unreadable_alpaca_refuses_every_leg_as_unprovable_after_one_attempt(account: _Account) -> None:
    """The batch's one pass cannot read Alpaca, so no bot can be proven flat: every
    leg is refused with the guard's own unprovable reason -- never an unknown
    outcome -- and Alpaca is asked once, not once per bot."""
    account.finished("spy-done-a", "spy-done-b")
    await account.requests_per_pass()
    account.alpaca.unreachable = True
    before = account.alpaca.requests

    result = await bot_clear.clear_bots("alpaca", ACCT, _request("spy-done-a", "spy-done-b"), operator_identity="owner")

    assert [leg.outcome for leg in result.legs] == ["refused", "refused"]
    assert {leg.error.reason_code for leg in result.legs if leg.error} == {"ARCHIVE_CUSTODY_UNPROVABLE"}
    assert account.alpaca.requests - before <= 2, "one pass's broker read, not one per bot"



async def test_a_dead_bot_whose_run_never_settled_stays_on_home_until_recovery_settles_it(
    account: _Account,
) -> None:
    """With Retire gone (#2578) Clear is the only way off Home. A bot whose
    process died before its run settled is refused with ``BOT_DUTY_NOT_SETTLED``
    and stays listed; its page offers no Retire. The account's next sweep pass
    settles it -- no restart (owner decision 2026-09-30, #2589) -- although its
    run was closed before that pass, and Clear's own batch pass came first;
    then it clears.
    """
    account.dead_before_its_run_settled("spy-dead-1")
    account.finished("spy-done-1")
    await account.requests_per_pass()

    panel = await panel_data_source.get_panel("alpaca", ACCT, "spy-dead-1")
    assert [action.action_id for action in panel.actions if action.action_id in {"retire", "archive"}] == [
        "archive"
    ]

    refused = await bot_clear.clear_bots(
        "alpaca", ACCT, _request("spy-dead-1", "spy-done-1"), operator_identity="owner"
    )

    assert [(leg.strategy_instance_id, leg.outcome) for leg in refused.legs] == [
        ("spy-dead-1", "refused"), ("spy-done-1", "applied"),
    ]
    dead = refused.legs[0].error
    assert dead is not None
    assert (dead.reason_code, dead.message) == (
        "BOT_DUTY_NOT_SETTLED", "This bot's last run has not finished settling.",
    )
    listed = [row.strategy_instance_id for row in await panel_data_source.get_catalog("alpaca", ACCT)]
    assert listed == ["spy-dead-1"]

    await ReconciliationSweep(
        repo=account.repo,
        read=account.alpaca,  # type: ignore[arg-type]
        trade=account.alpaca,  # type: ignore[arg-type]
        intake=account.facade.intake,
        run_ownership=account.facade.run_ownership,
        max_passes=1,
        sleep=lambda _delay: asyncio.sleep(0),
        pricing=UNPRICEABLE_RECOVERY,
        on_duty_settle=account.runner.settle_dead_runs,
    ).run()
    settled = await bot_clear.clear_bots(
        "alpaca", ACCT, _request("spy-dead-1", key="clear-2"), operator_identity="owner"
    )

    assert [(leg.strategy_instance_id, leg.outcome) for leg in settled.legs] == [("spy-dead-1", "applied")]
    assert await panel_data_source.get_catalog("alpaca", ACCT) == []


async def test_a_stopped_dry_run_clears_beside_an_account_bot_from_one_batch(account: _Account) -> None:
    """A Dry Run that has stopped since the service started has its simulated
    account closed. Asking whether such a bot is sealed on another account
    once needed that account open, and refused: nothing in the batch was
    cleared and the whole request failed (#2694). A Dry Run is never sealed
    on another account, and every leg gets its own answer."""
    account.finished("spy-done-1")
    await account.stopped_dry_run("dry-done-1")
    await account.requests_per_pass()

    result = await bot_clear.clear_bots(
        "alpaca", ACCT, _request("spy-done-1", "dry-done-1"), operator_identity="owner"
    )

    assert [(leg.strategy_instance_id, leg.outcome) for leg in result.legs] == [
        ("spy-done-1", "applied"), ("dry-done-1", "applied"),
    ], [leg.error for leg in result.legs if leg.error]
    assert account.runner.status("alpaca", "dry-done-1").phase == "RETIRED"

