"""Clearing a bot that rehearsed in a live account's Shadow world (#2694).

Such a bot stays sealed on ``shadow:<live_account>`` after the account goes
live. The installed Clerk holds no custody record for it, so it had no row on
Home and no page, and a Clear that named it answered the panel read's raw "No
custody record exists" with no reason code.

These tests pin what the owner now sees: a finished rehearsal bot in Home's
Finished fold -- listed by name, with no page to open -- a Clear that takes it
off through its own Shadow records, and a coded, plainly worded refusal for
one that still holds something. The proof itself, its arms and the retirement
are pinned where they live (``test_duty_settle``, ``test_recorded_custody``).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    activate_shadow_clerk_authority,
    set_active_clerk_runtime,
)
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeGate
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository, RepositoryPoisoned
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.contract.models import BrokerAccountSnapshot
from app.broker.contract.registry import reset_broker_registry_for_testing
from app.routers.broker_v2_panel import router
from app.schemas.broker_v2_panel import BotClearRequest, PanelActionRequest
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.bot_runner import BotTaskRegistry, set_bot_task_registry
from app.services.broker_v2_panel import bot_clear, bot_history, panel_data_source, panel_scope, sqlite_roster_status
from app.services.broker_v2_panel.action_execution_service import (
    ActionOutcomeUnknownError,
    reset_idempotency_store_for_testing,
)
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS
from tests.broker.alpaca.clerk.live_envelope_fixtures import TEST_ENVELOPE_VALUES
from tests.broker.alpaca.clerk.sqlite.conftest import _make_held_position
from tests.broker.v2panel.conftest import account_snapshot
from tests.services.test_bot_runner_ema_resume import _FlatBroker

_LIVE = "9LIVE0001"
_SEALED = f"shadow:{_LIVE}"
_FLAT = "spy-rehearsal-flat"
_HELD = "spy-rehearsal-held"
#: Sealed on another real account, which keeps no records this lane can read.
_ELSEWHERE = "spy-sealed-elsewhere"
#: What the panel read answered for such a bot, and what no Clear may answer now.
_RAW_PANEL_TEXT = "No custody record exists"
#: The Clear a stopped, settled bot presents (``test_archive_eligibility``).
_PRESENTED_CLEAR_TOKEN = "9a8f9b9530fb687e13bc07cfd1c5b9aa"


@dataclass(frozen=True)
class _Lane:
    """A graduated live lane: its own Clerk installed, its Shadow store beside it."""

    root: Path
    live: ClerkSqliteRepository
    runner: BotTaskRegistry

    def retired_at_ms(self, sid: str) -> int | None:
        """When the Shadow store's own record says the bot was cleared."""
        sealed = ClerkSqliteRepository.open(account_id=_SEALED, artifacts_root=self.root)
        try:
            return sealed.strategy_instance(sid)["retired_at_ms"]
        finally:
            sealed.close()


async def _rehearsed(root: Path, runner: BotTaskRegistry, sid: str, *, holds_spy: bool) -> None:
    """One bot that ran in the Shadow world and stopped; ``holds_spy`` leaves it long 10 SPY."""
    sealed = ClerkSqliteRepository.open(account_id=_SEALED, artifacts_root=root)
    try:
        sealed.register_strategy_instance(
            exit_terms=DEPLOY_EXIT_TERMS,
            strategy_instance_id=sid,
            symbol="SPY",
            config_hash="config-1",
            strategy_key="deployment_validation",
            display_name="Deployment Validation",
            config_json=json.dumps({"mode": "trade", "quantity": 1, "carryover_policy": "FORBID"}),
        )
        submit_start_run(sealed, account_id=_SEALED, strategy_instance_id=sid, lifecycle_run_id=f"run-{sid}")
        if holds_spy:
            await _make_held_position(sealed, account_id=_SEALED, strategy_instance_id=sid, run_id=f"run-{sid}")
        submit_stop_run(sealed, account_id=_SEALED, strategy_instance_id=sid, lifecycle_run_id=f"run-{sid}")
    finally:
        sealed.close()
    _bound(runner, sid, sealed_account_id=_SEALED)


def _bound(runner: BotTaskRegistry, sid: str, *, sealed_account_id: str) -> None:
    """The runner's own record of a stopped bot, sealed on ``sealed_account_id``."""
    runner._bindings.record_launch(
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
            sealed_account_id=sealed_account_id,
            exit_terms=DEPLOY_EXIT_TERMS,
        ),
        launch_reason="deploy",
    )


@pytest.fixture()
async def lane(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_Lane]:
    reset_broker_registry_for_testing()
    reset_idempotency_store_for_testing()
    root = tmp_path / "artifacts"

    async def _cached_account(_broker: str) -> BrokerAccountSnapshot:
        return account_snapshot(account_id=_LIVE, account_mode="live")

    monkeypatch.setattr(panel_scope, "resolve_account_snapshot", _cached_account)
    for module in (bot_history, sqlite_roster_status):
        monkeypatch.setattr(module, "live_artifacts_root", lambda: root)
    await activate_shadow_clerk_authority(live_account_id=_LIVE, artifacts_root=root)
    runner = BotTaskRegistry(root, feed_resolver=lambda: None, boot_recovery_required=False)
    await _rehearsed(root, runner, _FLAT, holds_spy=False)
    await _rehearsed(root, runner, _HELD, holds_spy=True)
    _bound(runner, _ELSEWHERE, sealed_account_id="PA-OTHER")
    live = ClerkSqliteRepository.initialize(account_id=_LIVE, artifacts_root=root)
    broker = _FlatBroker()
    facade = SqliteAlpacaClerkFacade(
        account_mode="live", repo=live, read=broker, trade=broker,
        live_envelope=LiveEnvelopeGate(values=TEST_ENVELOPE_VALUES, custody_is_simulated=False),
    )
    set_active_clerk_runtime(
        ActiveClerkRuntime(
            authority_kind="sqlite", clerk=facade, _sqlite_repository=live,
            account_id=_LIVE, account_authority_kind="real_live",
        )
    )
    set_bot_task_registry(runner)
    try:
        yield _Lane(root=root, live=live, runner=runner)
    finally:
        set_active_clerk_runtime(None)
        set_bot_task_registry(None)
        live.close()
        reset_broker_registry_for_testing()
        reset_idempotency_store_for_testing()


def _clear(*sids: str, key: str = "clear-1") -> BotClearRequest:
    return BotClearRequest(idempotency_key=key, strategy_instance_ids=list(sids))


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


async def test_home_lists_a_finished_rehearsal_bot_by_name_and_no_other_roster_does(lane: _Lane) -> None:
    """Finished, in the Shadow world's own words, with the reason it has no
    page. The bot still holding stays in History alone, and the roster every
    other reader gets -- the cohort flatten, the Wall's feed -- has neither."""
    home = {row.strategy_instance_id: row for row in await panel_data_source.get_catalog("alpaca", _LIVE, home=True)}

    assert set(home) == {_FLAT}
    row = home[_FLAT]
    assert (row.group, row.world_label, row.account_id) == (
        "finished", "SHADOW · simulated fills on your live account", _SEALED,
    )
    assert (row.phase, row.running, row.exposure, row.trade_count) == ("OFF_DUTY", False, {}, 0)
    assert row.page_unavailable_reason is not None
    assert row.page_unavailable_reason.startswith("This bot ran in the account's Shadow world")
    assert await panel_data_source.get_catalog("alpaca", _LIVE) == []


async def test_clearing_a_rehearsal_bot_from_home_takes_it_off_through_its_shadow_records(lane: _Lane) -> None:
    """The owner's Clear, as Home sends it. The flat bot is retired in the
    Shadow store and leaves Home; the installed Clerk is never written."""
    result = await bot_clear.clear_bots("alpaca", _LIVE, _clear(_FLAT), operator_identity="owner")

    assert [(leg.strategy_instance_id, leg.outcome) for leg in result.legs] == [(_FLAT, "applied")]
    assert lane.runner.status("alpaca", _FLAT).phase == "RETIRED"
    assert lane.retired_at_ms(_FLAT) is not None
    assert lane.live.strategy_instances() == []
    assert await panel_data_source.get_catalog("alpaca", _LIVE, home=True) == []

    again = await bot_clear.clear_bots("alpaca", _LIVE, _clear(_FLAT), operator_identity="owner")
    assert [leg.outcome for leg in again.legs] == ["replayed"]


async def test_a_clear_naming_a_holding_rehearsal_bot_is_refused_with_a_code_and_what_it_holds(lane: _Lane) -> None:
    """A Clear request that names the bot directly: its refusal is the guard's
    own sentence and code, never the panel read's raw text, and its sibling
    still clears."""
    result = await bot_clear.clear_bots("alpaca", _LIVE, _clear(_HELD, _FLAT), operator_identity="owner")

    assert [(leg.strategy_instance_id, leg.outcome) for leg in result.legs] == [(_HELD, "refused"), (_FLAT, "applied")]
    refused = result.legs[0].error
    assert refused is not None
    assert (refused.outcome, refused.reason_code) == ("conflict", "ARCHIVE_REHEARSAL_STILL_HOLDS")
    assert refused.message == "This bot's Shadow records still show 10 SPY."
    assert _RAW_PANEL_TEXT not in f"{refused.message} {refused.why}"
    assert lane.runner.status("alpaca", _HELD).phase == "OFF_DUTY"
    assert lane.retired_at_ms(_HELD) is None


async def test_a_clear_naming_a_bot_sealed_on_another_real_account_is_refused_for_its_account(lane: _Lane) -> None:
    """Only a Shadow store keeps records to read in the installed Clerk's
    place. Any other account's bot is refused for its account -- by code,
    where it too used to answer the panel read's raw text."""
    result = await bot_clear.clear_bots("alpaca", _LIVE, _clear(_ELSEWHERE), operator_identity="owner")

    refused = result.legs[0].error
    assert refused is not None
    assert (result.legs[0].outcome, refused.reason_code) == ("refused", "ARCHIVE_SEALED_ACCOUNT_CUSTODY")
    assert refused.message == "This bot's account is no longer managed here."
    assert lane.runner.status("alpaca", _ELSEWHERE).phase == "OFF_DUTY"


@pytest.mark.parametrize(
    ("action_id", "reason_code"),
    [("archive", "ARCHIVE_REHEARSAL_STILL_HOLDS"), ("reconcile_now", "BOT_SEALED_ON_ANOTHER_ACCOUNT")],
)
async def test_a_command_posted_for_a_rehearsal_bot_answers_with_a_code(
    action_id: str, reason_code: str, lane: _Lane,
) -> None:
    """The bot's own action route. Clear is the one command it takes, proved
    and refused by the same guard; every other command acts through the
    installed Clerk, which holds nothing of this bot's, and says so by code."""
    async with httpx.AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{_LIVE}/bots/{_HELD}/actions",
            json={
                "action_id": action_id,
                "revision": 0,
                "concurrency_token": _PRESENTED_CLEAR_TOKEN,
                "idempotency_key": f"direct-{action_id}",
            },
        )

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["reason_code"] == reason_code
    assert _RAW_PANEL_TEXT not in response.text
    assert lane.retired_at_ms(_HELD) is None


async def test_a_shadow_store_that_cannot_confirm_the_retirement_is_this_bots_unknown_outcome(
    lane: _Lane, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A poisoned store may already hold the retirement. That is the Shadow
    store's fence, not the installed account's: the bot's outcome is unknown,
    and the account's authority is never reported poisoned for it -- which
    would end a clear batch for every other bot."""

    async def _poisoned(*_args: object, **_kwargs: object) -> None:
        raise RepositoryPoisoned("mirror finalize unconfirmed")

    monkeypatch.setattr(lane.runner, "archive", _poisoned)

    with pytest.raises(ActionOutcomeUnknownError):
        await panel_data_source.run_action(
            "alpaca", _LIVE, _FLAT,
            PanelActionRequest(
                action_id="archive", revision=0, concurrency_token=_PRESENTED_CLEAR_TOKEN, idempotency_key="k-1",
            ),
            operator_identity="owner",
        )
