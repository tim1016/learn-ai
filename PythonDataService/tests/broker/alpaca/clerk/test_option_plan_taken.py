"""Which option plans the account Clerk takes: the one rule, its two gates and a real account's setting (#2913).

The rule is asked at registration, at the entry and by the preview. Each
test here asks it directly, and opens a gate itself where it needs one open.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.broker.alpaca.clerk import option_effects
from app.broker.alpaca.clerk.option_effects import OptionEffectRefusal, OptionPlanRefused, option_plan_taken
from app.config import settings
from app.schemas.action_plan import ActionPlan, OptionEntryLeg


def _leg(leg_id: str, *, position: str = "long", right: str = "call", ratio: int = 1, underlying: str = "SPY") -> dict[str, Any]:
    return {
        "leg_id": leg_id,
        "instrument": {"kind": "option", "underlying": underlying},
        "position": position,
        "qty_ratio": ratio,
        "right": right,
        "strike": {"selector": "atm"},
        "expiry": {"selector": "nearest_weekly"},
    }


def _plan(*legs: dict[str, Any]) -> ActionPlan:
    return ActionPlan.model_validate(
        {"on_enter": list(legs), "on_exit": [{"kind": "close_leg", "entry_leg_id": leg["leg_id"]} for leg in legs]}
    )


def _unchecked_plan(*legs: dict[str, Any]) -> ActionPlan:
    """A plan the Action Plan's own rules would refuse, built past them: the Clerk's rule is asked on its own."""
    return ActionPlan.model_construct(on_enter=[OptionEntryLeg.model_validate(leg) for leg in legs], on_exit=[])


ONE_CALL = _plan(_leg("long_call"))
STRADDLE = _plan(_leg("long_call"), _leg("long_put", right="put"))
SPREAD = _plan(_leg("long_call"), _leg("short_call", position="short"))


@pytest.fixture
def bought_legs_taken(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(option_effects, "BOUGHT_LEG_STRUCTURES_TAKEN", True)


@pytest.fixture
def sold_legs_taken(monkeypatch: pytest.MonkeyPatch, bought_legs_taken: None) -> None:
    monkeypatch.setattr(option_effects, "SOLD_LEG_STRUCTURES_TAKEN", True)


@pytest.fixture
def setting_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ALPACA_MULTI_LEG_OPTION_ORDERS_ENABLED", True)


def _refusal(plan: ActionPlan, *, real_account: bool) -> OptionPlanRefused:
    with pytest.raises(OptionPlanRefused) as refused:
        option_plan_taken(plan, real_account=real_account)
    return refused.value


@pytest.mark.parametrize("real_account", [True, False])
def test_one_bought_call_is_taken_on_every_account_and_answers_its_leg_and_its_underlying(real_account: bool) -> None:
    taken = option_plan_taken(ONE_CALL, real_account=real_account)

    assert [leg.leg_id for leg in taken.legs] == ["long_call"]
    assert taken.underlying == "SPY"


def test_both_gates_are_shut_until_the_pieces_that_open_them_are_built() -> None:
    assert option_effects.BOUGHT_LEG_STRUCTURES_TAKEN is False
    assert option_effects.SOLD_LEG_STRUCTURES_TAKEN is False
    assert settings.ALPACA_MULTI_LEG_OPTION_ORDERS_ENABLED is False


@pytest.mark.parametrize("plan", [STRADDLE, SPREAD], ids=["two bought legs", "a sold leg"])
@pytest.mark.parametrize("real_account", [True, False])
def test_with_nothing_opened_a_plan_of_several_legs_is_refused_under_one_code_on_every_account(
    plan: ActionPlan, real_account: bool
) -> None:
    assert _refusal(plan, real_account=real_account).reason is OptionEffectRefusal.STRUCTURE_NOT_SUPPORTED


def test_the_first_gate_opens_a_structure_of_bought_legs_and_keeps_a_sold_leg_refused(bought_legs_taken: None) -> None:
    taken = option_plan_taken(STRADDLE, real_account=False)

    assert [leg.leg_id for leg in taken.legs] == ["long_call", "long_put"]
    assert taken.underlying == "SPY"
    refused = _refusal(SPREAD, real_account=False)
    assert refused.reason is OptionEffectRefusal.STRUCTURE_NOT_SUPPORTED
    assert "short_call" in refused.detail


def test_the_second_gate_alone_opens_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(option_effects, "SOLD_LEG_STRUCTURES_TAKEN", True)

    assert _refusal(SPREAD, real_account=False).reason is OptionEffectRefusal.STRUCTURE_NOT_SUPPORTED
    assert _refusal(STRADDLE, real_account=False).reason is OptionEffectRefusal.STRUCTURE_NOT_SUPPORTED


def test_both_gates_open_a_structure_with_a_sold_leg_in_the_plans_own_leg_order(sold_legs_taken: None) -> None:
    taken = option_plan_taken(SPREAD, real_account=False)

    assert [(leg.leg_id, leg.position) for leg in taken.legs] == [("long_call", "long"), ("short_call", "short")]


def test_a_real_account_refuses_several_legs_under_the_settings_own_code_while_it_is_off(sold_legs_taken: None) -> None:
    refused = _refusal(SPREAD, real_account=True)

    assert refused.reason is OptionEffectRefusal.SEVERAL_LEGS_SETTING_OFF
    assert refused.reason.value == "OPTION_SEVERAL_LEGS_SETTING_OFF"
    # The refusal names the setting and says where the plan is taken.
    assert "ALPACA_MULTI_LEG_OPTION_ORDERS_ENABLED" in refused.detail and "Dry Run" in refused.detail


def test_a_real_account_takes_several_legs_once_its_setting_is_on(sold_legs_taken: None, setting_on: None) -> None:
    assert len(option_plan_taken(SPREAD, real_account=True).legs) == 2
    assert len(option_plan_taken(STRADDLE, real_account=True).legs) == 2


def test_the_setting_alone_opens_nothing_on_a_real_account(setting_on: None) -> None:
    assert _refusal(STRADDLE, real_account=True).reason is OptionEffectRefusal.STRUCTURE_NOT_SUPPORTED


def test_a_dry_run_never_reads_the_setting(sold_legs_taken: None, monkeypatch: pytest.MonkeyPatch) -> None:
    def never_asked() -> bool:
        pytest.fail("a simulation sends nothing, so it never asks the setting")

    monkeypatch.setattr(option_effects, "multi_leg_option_orders_enabled", never_asked)

    assert len(option_plan_taken(SPREAD, real_account=False).legs) == 2
    # Nor does a plan of one leg, on any account.
    assert len(option_plan_taken(ONE_CALL, real_account=True).legs) == 1


@pytest.mark.parametrize(
    ("plan", "said"),
    [
        pytest.param(
            _unchecked_plan(_leg("short_put", position="short", right="put")), "sells an option to open",
            id="a lone sold leg",
        ),
        pytest.param(_unchecked_plan(_leg("long_call", ratio=2)), "more structures", id="a lone leg of two contracts"),
        pytest.param(
            _unchecked_plan(_leg("long_call"), _leg("long_put", right="put", underlying="QQQ")), "one underlying",
            id="legs on two underlyings",
        ),
    ],
)
def test_what_no_gate_ever_opens_is_refused_by_what_it_is(sold_legs_taken: None, plan: ActionPlan, said: str) -> None:
    refused = _refusal(plan, real_account=False)

    assert refused.reason is OptionEffectRefusal.STRUCTURE_NOT_SUPPORTED
    assert said in refused.detail


def test_a_plan_in_shares_is_not_an_option_plan(sold_legs_taken: None) -> None:
    from app.services.bot_binding_repository import alpaca_v1_action_plan

    assert _refusal(alpaca_v1_action_plan("SPY"), real_account=False).reason is OptionEffectRefusal.STRUCTURE_NOT_SUPPORTED
