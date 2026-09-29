"""Clearing finished bots from Home (owner decision 2026-09-28, #2567).

Each leg is the unchanged per-bot ``archive`` (ADR 0052) run through the
shared batch executor, so what these tests pin is what clearing adds: the
request names bots and never an action, each leg runs under its own panel's
presented token and a derived identity, a leg the guard refuses carries the
guard's own reason while its siblings clear, and a resend replays.

The guard itself -- re-proved against fresh custody under the bot's lock --
is pinned where it lives (``test_registry_lifecycle``'s archive cases and
``test_archive_eligibility``); the catalog leaving cleared bots out is pinned
in ``test_sqlite_roster_source``.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from pydantic import ValidationError

from app.routers.broker_v2_panel import router
from app.schemas.broker_v2_panel import BotClearRequest, PanelActionRequest, PanelActionResult
from app.services.broker_v2_panel import bot_clear, panel_data_source
from app.services.broker_v2_panel.action_execution_service import ActionNotAvailableError
from app.services.broker_v2_panel.panel_errors import UnknownBotError
from tests.broker.v2panel.fixtures import ACCT

_FINISHED = "spy-done-1"
_DRY_RUN = "dry-done-1"
_HOLDING = "qqq-holding-1"
_RACED = "tsla-raced-1"

_STRAND_HEADLINE = "This bot still holds shares or has a working order."
_STRAND_WHY = "Flatten it and let its working orders finish, then clear it."


async def _accept_account(broker: str, account_id: str) -> str:
    return account_id


def _panel(sid: str, *, enabled: bool) -> SimpleNamespace:
    """A bot page's presentation: its archive action and that action's token."""
    return SimpleNamespace(actions=[
        SimpleNamespace(action_id="stop", revision=3, concurrency_token=f"stop-{sid}", enabled=False),
        SimpleNamespace(action_id="archive", revision=5, concurrency_token=f"token-{sid}", enabled=enabled),
    ])


class _Lane:
    """The per-bot pipeline, as the clear orchestration sees it.

    ``holding`` bots present a disabled archive and refuse it with the guard's
    reason; ``raced`` bots present it armed but a fill lands before the click,
    so the commit-time guard refuses. ``applied`` records every idempotency
    key the pipeline completed, so a resend replays instead of reapplying.
    """

    def __init__(self, *, holding: tuple[str, ...] = (), raced: tuple[str, ...] = ()) -> None:
        self.holding = holding
        self.raced = raced
        self.applied: set[str] = set()
        self.calls: list[tuple[str, str, str, str]] = []

    async def get_panel(self, broker: str, account_id: str, sid: str) -> SimpleNamespace:
        if sid == "not-a-bot":
            raise UnknownBotError(f"No bot '{sid}' is bound to broker '{broker}'.", detail="Refresh Home.")
        return _panel(sid, enabled=sid not in self.holding)

    async def run_action(
        self, broker: str, account_id: str, sid: str, request: PanelActionRequest, *, operator_identity: str,
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


@pytest.fixture()
def lane(monkeypatch: pytest.MonkeyPatch) -> _Lane:
    fake = _Lane(holding=(_HOLDING,), raced=(_RACED,))
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
            {"idempotency_key": "k", "strategy_instance_ids": [_FINISHED], "action_id": "flatten_stop"}
        )


def test_a_bot_is_cleared_once_per_request() -> None:
    with pytest.raises(ValidationError, match="once per request"):
        _request(_FINISHED, _FINISHED)


def test_the_derived_leg_identity_budget_is_enforced() -> None:
    with pytest.raises(ValidationError, match="identity budget"):
        BotClearRequest(idempotency_key="k" * 64, strategy_instance_ids=["s" * 96])


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
    result = await bot_clear.clear_bots("alpaca", ACCT, _request("not-a-bot", _FINISHED), operator_identity="owner")

    assert [(leg.strategy_instance_id, leg.outcome) for leg in result.legs] == [
        ("not-a-bot", "refused"), (_FINISHED, "applied"),
    ]
    assert [call[0] for call in lane.calls] == [_FINISHED]


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


async def test_the_commit_time_refusal_is_a_typed_refusal_not_an_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``bot_runner.archive`` refuses under the bot's lock before any write
    (ADR 0052 §3). The per-bot performer reports that as the guard's typed
    refusal -- nothing applied, key released for a retry -- instead of the
    "outcome unknown" every untyped performer error becomes."""
    from app.services.bot_runner_errors import BotRunnerError

    class _Registry:
        async def archive(self, broker: str, sid: str, *, updated_by: str, reason: str | None) -> None:
            raise BotRunnerError(_STRAND_HEADLINE, detail=_STRAND_WHY, reason_code="ARCHIVE_WOULD_STRAND_CUSTODY")

    monkeypatch.setattr(panel_data_source, "get_bot_task_registry", lambda: _Registry())
    archive = panel_data_source._action_performers("alpaca", _RACED, idempotency_key="clear-1:x")["archive"]

    with pytest.raises(ActionNotAvailableError) as refused:
        await archive("owner", "Cleared from Home")

    assert (str(refused.value), refused.value.detail) == (_STRAND_HEADLINE, _STRAND_WHY)
    assert refused.value.reason_code == "ARCHIVE_WOULD_STRAND_CUSTODY"
