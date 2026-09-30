"""The bot end's HTTP surface (#2607): the Deploy form's end and the bot panel's.

Deploy sends the owner's end with the bot's settings -- or none, for the
default end -- and the backend validates it before the bot is named. The end
never enters the settings' fingerprint, so it changes no seal or consent. The
bot panel shows a bot's end with its panel (``BotPanelView.end``) and changes
it on a running bot. The pinned clock is 09:31 ET on Fri 2026-09-25
(``conftest``).
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
from app.utils.session_anchors import MAX_TIMESTAMP_MS
from app.utils.timestamps import to_ms_utc
from tests.broker.v2panel.conftest import _BODY, _SETTINGS, _T0
from tests.broker.v2panel.fixtures import ACCT

_ET = ZoneInfo("America/New_York")
_FRIDAY = date(2026, 9, 25)
_ALLOW_BODY_STRATEGY = frozenset({("ema_crossover_signal", ACCT)})
_BOTS = f"/api/brokers/alpaca/accounts/{ACCT}/bots"


def _at(hour: int, minute: int = 0, *, day: date = _FRIDAY) -> int:
    return to_ms_utc(datetime(day.year, day.month, day.day, hour, minute, tzinfo=_ET))


def _end(end_at_ms: int | None, action: str = "SELL") -> dict:
    return {"end_at_ms": end_at_ms, "end_action": action}


@pytest.fixture
def allow_body_strategy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS", _ALLOW_BODY_STRATEGY)


def _client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ── Deploy ───────────────────────────────────────────────────────────────────


async def test_a_deploy_with_no_end_gets_todays_close_minus_one_minute(deploy_app, allow_body_strategy) -> None:
    app, registry = deploy_app

    async with _client(app) as client:
        response = await client.post(_BOTS, json=_BODY)

    assert response.status_code == 201
    assert registry.deploy_calls[-1]["end"] == BotEnd(end_at_ms=_at(15, 59), end_action="SELL")


async def test_a_deploy_can_ask_for_no_end(deploy_app, allow_body_strategy) -> None:
    app, registry = deploy_app

    async with _client(app) as client:
        response = await client.post(_BOTS, json={**_BODY, "end": _end(None)})

    assert response.status_code == 201
    assert registry.deploy_calls[-1]["end"] is None


async def test_a_deploy_refuses_an_explicit_null_end(deploy_app, allow_body_strategy) -> None:
    """#2607 review: an omitted end is the default; ``"end": null`` is no choice at all, so it is refused."""
    app, registry = deploy_app

    async with _client(app) as client:
        response = await client.post(_BOTS, json={**_BODY, "end": None})

    assert response.status_code == 422
    assert "end may not be null" in response.text
    assert registry.deploy_calls == []


async def test_a_deploy_end_must_say_sell_or_keep(deploy_app, allow_body_strategy) -> None:
    """#2607 review: an end naming only its time never defaults to SELL."""
    app, registry = deploy_app

    async with _client(app) as client:
        response = await client.post(_BOTS, json={**_BODY, "end": {"end_at_ms": _at(12, 30)}})

    assert response.status_code == 422
    assert registry.deploy_calls == []


async def test_a_deploy_records_the_owners_end_and_action(deploy_app, allow_body_strategy) -> None:
    app, registry = deploy_app

    async with _client(app) as client:
        response = await client.post(_BOTS, json={**_BODY, "end": _end(_at(12, 30), "KEEP")})

    assert response.status_code == 201
    assert registry.deploy_calls[-1]["end"] == BotEnd(end_at_ms=_at(12, 30), end_action="KEEP")


async def test_a_deploy_ending_after_the_close_is_refused_in_plain_words_and_starts_nothing(
    deploy_app, allow_body_strategy,
) -> None:
    app, registry = deploy_app

    async with _client(app) as client:
        response = await client.post(_BOTS, json={**_BODY, "end": _end(_at(17))})

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["message"] == "The end must fall within regular hours."
    assert detail["why"] == (
        "On Fri Sep 25 the market is open from 09:30 to 16:00 ET, so the latest end is 15:59 ET."
    )
    assert detail["reason_code"] == "BOT_END_REFUSED"
    assert detail["submission_settled"] is True
    assert registry.deploy_calls == []


@pytest.mark.parametrize(
    "end_at_ms",
    [
        pytest.param(to_ms_utc(datetime(2263, 6, 17, 15, 0, tzinfo=_ET)), id="year-2263"),
        pytest.param(MAX_TIMESTAMP_MS, id="max-timestamp"),
    ],
)
async def test_a_deploy_ending_past_the_calendar_is_refused_not_a_500(
    deploy_app, allow_body_strategy, end_at_ms: int,
) -> None:
    """#2607 review: every instant the contract admits is answered in words."""
    app, registry = deploy_app

    async with _client(app) as client:
        response = await client.post(_BOTS, json={**_BODY, "end": _end(end_at_ms)})

    assert response.status_code == 400
    assert response.json()["detail"]["message"] == "The market calendar doesn't cover that date."
    assert response.json()["detail"]["reason_code"] == "BOT_END_REFUSED"
    assert registry.deploy_calls == []


async def test_a_deploy_keeping_with_no_end_is_refused(deploy_app, allow_body_strategy) -> None:
    app, registry = deploy_app

    async with _client(app) as client:
        response = await client.post(_BOTS, json={**_BODY, "end": _end(None, "KEEP")})

    assert response.status_code == 400
    assert response.json()["detail"]["message"] == "A bot with no end has no shares to keep at it."
    assert registry.deploy_calls == []


async def test_a_dry_run_deploy_that_keeps_its_shares_is_refused(deploy_app) -> None:
    app, registry = deploy_app

    async with _client(app) as client:
        response = await client.post(
            _BOTS, json={**_BODY, "execution_mode": "dry_run", "end": _end(_at(15, 59), "KEEP")},
        )

    assert response.status_code == 400
    assert response.json()["detail"]["why"] == (
        "A Dry Run never ends holding; it always sells at the last price it saw."
    )
    assert registry.deploy_calls == []


def test_the_end_never_enters_the_settings_fingerprint() -> None:
    """No seal or consent changes with the end: the owner's schedule is not a sealed term."""
    without = AlpacaPaperDeployRequest.model_validate(_SETTINGS)
    with_end = AlpacaPaperDeployRequest.model_validate({**_SETTINGS, "end": _end(_at(12, 30), "KEEP")})

    assert with_end.fingerprint(account=ACCT) == without.fingerprint(account=ACCT)


async def test_the_deploy_view_offers_the_default_end(deploy_app) -> None:
    app, _registry = deploy_app

    async with _client(app) as client:
        response = await client.get(f"{_BOTS}/deploy")

    assert response.status_code == 200
    default_end = response.json()["default_end"]
    assert default_end["end_at_ms"] == _at(15, 59)
    assert default_end["end_action"] == "SELL"
    assert default_end["headline"] == "Ends Fri Sep 25, 15:59 ET · sells"


async def test_the_start_admission_preview_refuses_the_end_a_deploy_would_refuse(
    deploy_app, allow_body_strategy,
) -> None:
    """#2607 review: the preview answers what the Deploy would, end included."""
    app, _registry = deploy_app

    async with _client(app) as client:
        response = await client.post(f"{_BOTS}/admission", json={**_SETTINGS, "end": _end(_at(17))})

    assert response.status_code == 400
    assert response.json()["detail"]["message"] == "The end must fall within regular hours."
    assert response.json()["detail"]["reason_code"] == "BOT_END_REFUSED"


# ── the Deploy form's check ──────────────────────────────────────────────────


async def test_the_end_preview_shows_a_half_day_clamp(deploy_app) -> None:
    app, _registry = deploy_app
    half_day = date(2026, 11, 27)

    async with _client(app) as client:
        response = await client.post(
            f"{_BOTS}/end-preview", json={"execution_mode": "paper", "end": _end(_at(15, 59, day=half_day))},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["end_at_ms"] == _at(12, 59, day=half_day)
    assert body["notice"] == "Fri Nov 27 closes early at 13:00 ET, so this bot ends at 12:59 ET."
    assert body["headline"] == "Ends Fri Nov 27, 12:59 ET · sells"


async def test_the_end_preview_with_no_end_chosen_is_the_default(deploy_app) -> None:
    app, _registry = deploy_app

    async with _client(app) as client:
        response = await client.post(f"{_BOTS}/end-preview", json={"execution_mode": "paper"})

    assert response.status_code == 200
    assert response.json()["end_at_ms"] == _at(15, 59)


async def test_the_end_preview_refuses_an_explicit_null_end(deploy_app) -> None:
    app, _registry = deploy_app

    async with _client(app) as client:
        response = await client.post(f"{_BOTS}/end-preview", json={"execution_mode": "paper", "end": None})

    assert response.status_code == 422
    assert "end may not be null" in response.text


async def test_the_end_preview_refuses_in_plain_words_with_its_code(deploy_app) -> None:
    app, _registry = deploy_app

    async with _client(app) as client:
        response = await client.post(f"{_BOTS}/end-preview", json={"execution_mode": "paper", "end": _end(_at(8))})

    assert response.status_code == 400
    assert response.json()["detail"]["message"] == "The end must fall within regular hours."
    assert response.json()["detail"]["reason_code"] == "BOT_END_REFUSED"


# ── the bot panel's end ──────────────────────────────────────────────────────


_VIEW = BotEndView(
    end_at_ms=_at(15, 59), end_action="SELL", status="scheduled",
    headline="Ends Fri Sep 25, 15:59 ET · sells", explanation="…", editable=True,
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


async def test_the_panel_changes_a_running_bots_end(end_app) -> None:
    app, registry = end_app

    async with _client(app) as client:
        response = await client.put(f"{_BOTS}/end-bot/end", json=_end(_at(14), "KEEP"))

    assert response.status_code == 200
    assert response.json()["end_at_ms"] == _at(14)
    [(sid, choice, updated_by)] = registry.edits
    assert (sid, choice) == ("end-bot", BotEndInput(end_at_ms=_at(14), end_action="KEEP"))
    assert updated_by  # the configured operator identity, never a request field


async def test_a_refused_edit_says_why_with_its_code(end_app) -> None:
    app, registry = end_app
    registry.refusal = BotEndRefused(
        "This bot has stopped, so it has no end to change.", detail="d", next_action="n", http_status=409,
    )

    async with _client(app) as client:
        response = await client.put(f"{_BOTS}/end-bot/end", json=_end(None))

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "message": "This bot has stopped, so it has no end to change.", "why": "d", "next_action": "n",
        "reason_code": "BOT_END_REFUSED",
    }


async def test_an_unknown_bots_end_edit_is_404(end_app) -> None:
    app, _registry = end_app

    async with _client(app) as client:
        response = await client.put(f"{_BOTS}/other-bot/end", json=_end(None))

    assert response.status_code == 404


async def test_a_bots_end_is_read_with_its_panel_not_on_its_own(end_app) -> None:
    """#2607 review: the unused ``GET .../end`` is gone; the panel carries the end."""
    app, _registry = end_app

    async with _client(app) as client:
        response = await client.get(f"{_BOTS}/end-bot/end")

    assert response.status_code == 405


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"end_action": "SELL"}, id="no-time"),
        pytest.param({"end_at_ms": _at(14)}, id="no-sell-or-keep"),
    ],
)
async def test_an_edit_must_name_the_end_and_its_action(end_app, body: dict) -> None:
    """Both are required: ``null`` is the explicit "no end", and a time alone never flips KEEP to SELL."""
    app, registry = end_app

    async with _client(app) as client:
        response = await client.put(f"{_BOTS}/end-bot/end", json=body)

    assert response.status_code == 422
    assert registry.edits == []
