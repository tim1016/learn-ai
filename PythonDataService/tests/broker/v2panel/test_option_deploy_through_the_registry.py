"""The click, then the real registry's start, with exactly what the route handed over (#2913).

The route's own tests stop at a registry that records keyword arguments and
takes any. Here the recorded arguments are given, unchanged, to the real
``BotTaskRegistry`` on a Dry Run, and what it stored is read back: the
instance record, the seal, the Clerk's own copy of the configuration, and
the binding the runner reads its terms from. Each carries the owner's four
order terms and the plan the Deploy built. Nothing is sent to a broker.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.active_authority import get_clerk_runtime, reset_alpaca_clerk_for_testing
from app.schemas.action_plan import ActionPlan
from app.schemas.option_order_terms import SealedOptionOrderTerms
from app.services import bot_option_run
from app.services.bot_runner import BotTaskRegistry
from app.services.bot_runner_errors import (
    OPTION_BOT_REGULAR_HOURS_ONLY,
    OPTION_ORDER_TERMS_NOT_APPLICABLE,
    OPTION_ORDER_TERMS_REQUIRED,
    TRADED_ASSET_ACKNOWLEDGEMENT_REQUIRED,
    BotRunnerError,
    UnknownBotError,
)
from tests._helpers.bot_runner.custody import _registry
from tests._helpers.bot_runner.doubles import _FakeClerk, _FakeFeed
from tests.broker.v2panel.conftest import _BODY
from tests.broker.v2panel.test_deploy_option_structure import (
    _ONE_CALL,
    _deploy,
    _Floor,
    _the_quotes_are_up,
    floor,  # noqa: F401 -- pytest fixture, used by name
)
from tests.services.bot_runner._support import _install_fake_clerk

_TERMS = {"gap_limit_percent": "12.5", "entry_wait_seconds": 7, "cost_limit_percent": "4", "expiry_close_lead_minutes": 90}


@pytest.fixture
def own_synthetic_authority() -> Iterator[None]:
    reset_alpaca_clerk_for_testing()
    yield
    reset_alpaca_clerk_for_testing()


async def _what_the_click_handed_over(app, fake, floor: _Floor) -> dict[str, Any]:  # noqa: F811
    await _the_quotes_are_up(floor)
    # The review was made at the simulator's own risk revision, as the real store judges the consent against.
    response = await _deploy(
        app, execution_mode="dry_run", trades={**_ONE_CALL, "order_terms": _TERMS}, sizing={"preset": "custom", "quantity": 3},
        budget={**_BODY["budget"], "risk_revision": 0},
    )
    assert response.status_code == 201, response.text
    [call] = fake.deploy_calls
    return call


def _the_real_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> BotTaskRegistry:
    _install_fake_clerk(monkeypatch, _FakeClerk())
    return _registry(tmp_path / "real", _FakeFeed([], mode="hold"))


async def _stop_if_running(registry: BotTaskRegistry, sid: str) -> None:
    try:
        await registry.stop("alpaca", sid)
    except UnknownBotError:
        return  # its run had already ended


async def test_what_the_click_hands_over_is_taken_by_the_real_registry_and_stored_whole(
    deploy_app, floor: _Floor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, own_synthetic_authority  # noqa: F811
) -> None:
    app, fake = deploy_app
    call = await _what_the_click_handed_over(app, fake, floor)
    plan, sid = call["action_plan"], call["strategy_instance_id"]
    assert isinstance(plan, ActionPlan) and call["option_order_terms"] == SealedOptionOrderTerms.model_validate(_TERMS)
    without_its_end = {name: value for name, value in call.items() if name != "end"}
    # Every keyword the route hands over is one the real start takes, and its admission preview too.
    inspect.signature(BotTaskRegistry.deploy_with_admission).bind(None, **call)
    inspect.signature(BotTaskRegistry.preview_start_admission).bind(None, **without_its_end)

    real = _the_real_registry(tmp_path, monkeypatch)
    # The run the start commits to is on the market the review priced the structure on.
    monkeypatch.setattr(bot_option_run, "this_process_option_market", floor.market)
    previewed = await real.preview_start_admission(**without_its_end)
    started = await real.deploy_with_admission(**call)

    try:
        assert previewed.allowed and started.admission.allowed
        assert (started.bot.mode, started.bot.quantity) == ("dry_run", 3)
        instance_dir = tmp_path / "real" / "live_state" / sid
        instance = json.loads((instance_dir / "strategy_instance.json").read_text())
        seal = json.loads((instance_dir / "sealed_program_v2.json").read_text())
        binding = real._bindings.read(sid)
        assert binding is not None
        runtime = get_clerk_runtime(f"sim:{sid}")
        assert runtime is not None and runtime.clerk is not None
        config = runtime.clerk.repository.bot_config(sid)
        assert config is not None
        registered = runtime.clerk.repository.strategy_instance(sid)
        assert registered is not None

        # The four terms, in each of the four places a start writes them.
        assert instance["option_order_terms"] == _TERMS
        assert seal["option_order_terms"] == _TERMS and seal["quantity"] == 3
        assert json.loads(config.config_json)["option_order_terms"] == _TERMS
        assert binding.option_order_terms == SealedOptionOrderTerms.model_validate(_TERMS)
        # The plan, in each place that carries one.
        handed = plan.model_dump(mode="json")
        assert instance["action_plan"] == handed and binding.action_plan == plan
        assert json.loads(config.config_json)["action_plan"] == handed
        assert registered["symbol"] == "SPY", "the Clerk registers an option bot under its contracts' underlying"
        # One configuration: the one the admission preview judged is the one the start stored.
        assert instance["configuration_hash"] == previewed.configuration_hash == started.admission.configuration_hash
        # And the runner's one read of a bot's order terms gives the owner's four numbers back.
        read = bot_option_run.option_order_terms(binding)
        assert (read.leg_gap_limit_percent, read.cost_limit_percent) == (Decimal("12.5"), Decimal(4))
        assert (read.close.entry_wait_ms, read.close.expiry_close_lead_ms) == (7_000, 90 * 60_000)
    finally:
        await _stop_if_running(real, sid)


@pytest.mark.parametrize(
    ("wrong", "refused_as"),
    [
        ({"option_order_terms": None}, OPTION_ORDER_TERMS_REQUIRED),
        ({"use_rth": False}, OPTION_BOT_REGULAR_HOURS_ONLY),
        ({"action_plan": None}, OPTION_ORDER_TERMS_NOT_APPLICABLE),
        ({"mode": "trade"}, TRADED_ASSET_ACKNOWLEDGEMENT_REQUIRED),
        ({"traded_symbol": "QQQ"}, "ACTION_PLAN_AND_TRADED_SYMBOL"),
    ],
    ids=["no terms", "extended hours", "terms and a plan in shares", "trade mode with no acceptance", "a plan and a traded symbol"],
)
async def test_the_real_registry_refuses_the_same_hand_over_with_one_thing_wrong_by_name(
    deploy_app, floor: _Floor, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, own_synthetic_authority,  # noqa: F811
    wrong: dict[str, Any], refused_as: str,
) -> None:
    app, fake = deploy_app
    call = await _what_the_click_handed_over(app, fake, floor)
    real = _the_real_registry(tmp_path, monkeypatch)

    with pytest.raises(BotRunnerError) as refused:
        await real.deploy_with_admission(**{**call, **wrong})

    assert refused.value.reason_code == refused_as
    assert not (tmp_path / "real" / "live_state" / call["strategy_instance_id"]).exists(), "nothing was stored"
