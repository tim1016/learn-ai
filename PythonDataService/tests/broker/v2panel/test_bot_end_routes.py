"""The bot end's HTTP surface (#2607): the Deploy form's end and the bot panel's.

Deploy sends the owner's end with the bot's settings -- or none, for the
default end -- and the backend validates it before the bot is named. The end
never enters the Deploy's fingerprint, so it changes no seal or consent. The
bot panel reads a bot's end in backend-authored words and changes it on a
running bot. The pinned clock is 09:31 ET on Fri 2026-09-25 (``conftest``).
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from httpx import ASGITransport

from app.schemas.bot_end import BotEnd, BotEndInput, BotEndView
from app.schemas.broker_bots import AlpacaPaperDeployRequest, BotStatusView
from app.services.bot_end import BotEndRefused
from app.services.bot_runner import UnknownBotError
from app.utils.timestamps import to_ms_utc
from tests.broker.v2panel.conftest import _BODY, _SETTINGS, _T0
from tests.broker.v2panel.fixtures import ACCT

_ET = ZoneInfo("America/New_York")
_FRIDAY = date(2026, 9, 25)
_ALLOW_BODY_STRATEGY = frozenset({("ema_crossover_signal", ACCT)})


def _at(hour: int, minute: int = 0, *, day: date = _FRIDAY) -> int:
    return to_ms_utc(datetime(day.year, day.month, day.day, hour, minute, tzinfo=_ET))


@pytest.fixture
def allow_body_strategy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS", _ALLOW_BODY_STRATEGY)


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ── Deploy ───────────────────────────────────────────────────────────────────


async def test_a_deploy_with_no_end_gets_todays_close_minus_one_minute(deploy_app, allow_body_strategy) -> None:
    app, registry = deploy_app

    async with _client(app) as client:
        response = await client.post(f"/api/brokers/alpaca/accounts/{ACCT}/bots", json=_BODY)

    assert response.status_code == 201
    assert registry.deploy_calls[-1]["end"] == BotEnd(end_at_ms=_at(15, 59), end_action="SELL")


async def test_a_deploy_can_ask_for_no_end(deploy_app, allow_body_strategy) -> None:
    app, registry = deploy_app

    async with _client(app) as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots", json={**_BODY, "end": {"end_at_ms": None}},
        )

    assert response.status_code == 201
    assert registry.deploy_calls[-1]["end"] is None


async def test_a_deploy_records_the_owners_end_and_action(deploy_app, allow_body_strategy) -> None:
    app, registry = deploy_app
    end = {"end_at_ms": _at(12, 30), "end_action": "KEEP"}

    async with _client(app) as client:
        response = await client.post(f"/api/brokers/alpaca/accounts/{ACCT}/bots", json={**_BODY, "end": end})

    assert response.status_code == 201
    assert registry.deploy_calls[-1]["end"] == BotEnd(end_at_ms=_at(12, 30), end_action="KEEP")


async def test_a_deploy_ending_after_the_close_is_refused_in_plain_words_and_starts_nothing(
    deploy_app, allow_body_strategy,
) -> None:
    app, registry = deploy_app

    async with _client(app) as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots", json={**_BODY, "end": {"end_at_ms": _at(17)}},
        )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["message"] == "The end must fall within regular hours."
    assert detail["why"] == (
        "On Fri Sep 25 the market is open from 09:30 to 16:00 ET, so the latest end is 15:59 ET."
    )
    assert detail["reason_code"] == "BOT_END_REFUSED"
    assert detail["submission_settled"] is True
    assert registry.deploy_calls == []


async def test_a_dry_run_deploy_that_keeps_its_shares_is_refused(deploy_app) -> None:
    app, registry = deploy_app

    async with _client(app) as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots",
            json={**_BODY, "execution_mode": "dry_run", "end": {"end_at_ms": _at(15, 59), "end_action": "KEEP"}},
        )

    assert response.status_code == 400
    assert response.json()["detail"]["why"] == (
        "A Dry Run never ends holding; it always sells at the last price it saw."
    )
    assert registry.deploy_calls == []


def test_the_end_never_enters_the_deploy_fingerprint() -> None:
    """No seal, consent or resend check changes with the end: the owner's schedule is not a sealed term."""
    without = AlpacaPaperDeployRequest.model_validate(_SETTINGS)
    with_end = AlpacaPaperDeployRequest.model_validate(
        {**_SETTINGS, "end": {"end_at_ms": _at(12, 30), "end_action": "KEEP"}}
    )

    assert with_end.fingerprint(account=ACCT) == without.fingerprint(account=ACCT)


async def test_the_deploy_view_offers_the_default_end(deploy_app) -> None:
    app, _registry = deploy_app

    async with _client(app) as client:
        response = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/deploy")

    assert response.status_code == 200
    default_end = response.json()["default_end"]
    assert default_end["end_at_ms"] == _at(15, 59)
    assert default_end["end_action"] == "SELL"
    assert default_end["headline"] == "Ends today 15:59 ET · sells"


# ── the Deploy form's check ──────────────────────────────────────────────────


async def test_the_end_preview_shows_a_half_day_clamp(deploy_app) -> None:
    app, _registry = deploy_app
    half_day = date(2026, 11, 27)

    async with _client(app) as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/end-preview",
            json={"execution_mode": "paper", "end": {"end_at_ms": _at(15, 59, day=half_day)}},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["end_at_ms"] == _at(12, 59, day=half_day)
    assert body["notice"] == "Fri Nov 27 closes early at 13:00 ET, so this bot ends at 12:59 ET."
    assert body["headline"] == "Ends Fri Nov 27 12:59 ET · sells"


async def test_the_end_preview_with_no_end_chosen_is_the_default(deploy_app) -> None:
    app, _registry = deploy_app

    async with _client(app) as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/end-preview", json={"execution_mode": "paper"},
        )

    assert response.status_code == 200
    assert response.json()["end_at_ms"] == _at(15, 59)


async def test_the_end_preview_refuses_in_plain_words(deploy_app) -> None:
    app, _registry = deploy_app

    async with _client(app) as client:
        response = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/end-preview",
            json={"execution_mode": "paper", "end": {"end_at_ms": _at(8)}},
        )

    assert response.status_code == 400
    assert response.json()["detail"]["message"] == "The end must fall within regular hours."


# ── the bot panel's end ──────────────────────────────────────────────────────


_VIEW = BotEndView(
    end_at_ms=_at(15, 59), end_action="SELL", status="scheduled",
    headline="Ends today 15:59 ET · sells", explanation="…", editable=True,
)


class _EndRegistry:
    """The runner's end surface, as the panel reaches it."""

    def __init__(self, deployed) -> None:
        self._deployed = deployed
        self.artifacts_root = deployed.artifacts_root
        self.edits: list[tuple[str, BotEndInput, str]] = []
        self.refusal: BotEndRefused | None = None

    def bindings_for_broker(self, broker: str) -> list:
        return []

    def status(self, broker: str, sid: str) -> BotStatusView:
        if sid != "end-bot":
            raise UnknownBotError(f"No bot '{sid}' is bound to broker '{broker}'.", detail="Deploy the bot first.")
        return BotStatusView(
            strategy_instance_id=sid, broker=broker, symbol="SPY", mode="trade", quantity=1, running=True,
            phase="ON_DUTY", desired_state="RUNNING", active_run_id="run-1", duty_outcome=None,
            binding_created_at_ms=_T0, last_transition_at_ms=None,
        )

    def bot_end(self, broker: str, sid: str) -> BotEndView:
        self.status(broker, sid)
        return _VIEW

    async def edit_bot_end(self, broker: str, sid: str, choice: BotEndInput, *, updated_by: str) -> BotEndView:
        self.status(broker, sid)
        if self.refusal is not None:
            raise self.refusal
        self.edits.append((sid, choice, updated_by))
        return _VIEW.model_copy(update={"end_at_ms": choice.end_at_ms, "end_action": choice.end_action})


@pytest.fixture
def end_app(deploy_app):
    from app.services.bot_runner import set_bot_task_registry

    app, deployed = deploy_app
    registry = _EndRegistry(deployed)
    set_bot_task_registry(registry)  # type: ignore[arg-type]
    return app, registry


async def test_the_panel_reads_a_bots_end(end_app) -> None:
    app, _registry = end_app

    async with _client(app) as client:
        response = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/end-bot/end")

    assert response.status_code == 200
    assert response.json()["headline"] == "Ends today 15:59 ET · sells"


async def test_the_panel_changes_a_running_bots_end(end_app) -> None:
    app, registry = end_app

    async with _client(app) as client:
        response = await client.put(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/end-bot/end",
            json={"end_at_ms": _at(14), "end_action": "KEEP"},
        )

    assert response.status_code == 200
    assert response.json()["end_at_ms"] == _at(14)
    [(sid, choice, updated_by)] = registry.edits
    assert (sid, choice) == ("end-bot", BotEndInput(end_at_ms=_at(14), end_action="KEEP"))
    assert updated_by  # the configured operator identity, never a request field


async def test_a_refused_edit_says_why(end_app) -> None:
    app, registry = end_app
    registry.refusal = BotEndRefused(
        "This bot has stopped, so it has no end to change.", detail="d", next_action="n", http_status=409,
    )

    async with _client(app) as client:
        response = await client.put(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/end-bot/end", json={"end_at_ms": None},
        )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "message": "This bot has stopped, so it has no end to change.", "why": "d", "next_action": "n",
    }


async def test_an_unknown_bots_end_is_404(end_app) -> None:
    app, _registry = end_app

    async with _client(app) as client:
        response = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/other-bot/end")

    assert response.status_code == 404


async def test_an_edit_must_name_the_end_explicitly(end_app) -> None:
    """``end_at_ms`` is required: ``null`` is the explicit "no end", never an omission."""
    app, registry = end_app

    async with _client(app) as client:
        response = await client.put(
            f"/api/brokers/alpaca/accounts/{ACCT}/bots/end-bot/end", json={"end_action": "SELL"},
        )

    assert response.status_code == 422
    assert registry.edits == []
