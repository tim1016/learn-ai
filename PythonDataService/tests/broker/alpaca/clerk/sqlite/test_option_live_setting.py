"""A Live account takes an option bot only while one named setting is on, and a Live Shadow never does (#2913).

The account Clerk is the second fence, under Deploy's: it refuses at
registration and at every effect, and reads the setting at each ask. The
Alpaca here is the stand-in port these books' tests use. Nothing is sent to
a broker.
"""

from __future__ import annotations

import pytest

from app.broker.alpaca.clerk.models import EffectOperationState, EffectPurpose
from app.broker.alpaca.clerk.option_effects import OptionEntryEvidence, OptionPlanRefused
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.config import Settings, settings
from app.schemas.option_order_terms import SealedOptionOrderTerms
from app.services.bot_binding_repository import BrokerBotBinding
from tests.broker.alpaca.clerk.sqlite.conftest import documented_option_listing
from tests.broker.alpaca.clerk.sqlite.test_option_entry_and_exit import (
    BUYS_ONE_CALL,
    CALL,
    EIGHT,
    GENERATION,
    LEG,
    NOON,
    TEN,
    TERMS,
    _live_clerk,
    _OneAccount,
    _quote,
    one_account,  # noqa: F401 -- pytest fixture, used by name
)

pytestmark = pytest.mark.usefixtures("legacy_run_registration")

_SETTING = "ALPACA_LIVE_OPTION_BOTS_ENABLED"
_SEALED = SealedOptionOrderTerms(
    gap_limit_percent=EIGHT, entry_wait_seconds=20, cost_limit_percent=TEN, expiry_close_lead_minutes=45
)


def _binding(account: _OneAccount, sid: str = "options-on-live") -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id=sid, strategy_key="ema_crossover_signal", broker="alpaca", symbol="SPY",
        mode="trade", quantity=1, action_plan=BUYS_ONE_CALL, option_order_terms=_SEALED, run_id="run-1",
        created_at_ms=0, sealed_account_id=account.repo.account_id, exit_terms=TERMS,
    )


def _shadow_clerk(account: _OneAccount) -> SqliteAlpacaClerkFacade:
    """The same books behind a Clerk whose authority is a Live Shadow's."""
    clerk = _live_clerk(account)
    clerk.authority_kind = "shadow"
    return clerk


def test_the_setting_is_off_unless_a_deployment_turns_it_on() -> None:
    """The default the code ships, not what this machine's environment happens to say."""
    assert Settings.model_fields[_SETTING].default is False


async def test_with_the_setting_off_a_live_clerk_refuses_the_plan_and_says_what_to_do(
    one_account: _OneAccount,  # noqa: F811
) -> None:
    with pytest.raises(OptionPlanRefused) as refused:
        await _live_clerk(one_account).register_strategy_run(_binding(one_account))

    assert refused.value.reason.value == "OPTION_PLAN_ACCOUNT_NOT_OPEN"
    assert _SETTING in refused.value.detail and "Paper" in refused.value.detail
    assert one_account.repo.strategy_instance("options-on-live") is None


async def test_turning_the_setting_on_is_all_a_live_clerk_needs_to_take_the_plan(
    one_account: _OneAccount, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """The same Clerk, the same plan, the same books: only the setting differs."""
    clerk = _live_clerk(one_account)
    with pytest.raises(OptionPlanRefused):
        clerk.require_option_plan_taken(BUYS_ONE_CALL)

    monkeypatch.setattr(settings, _SETTING, True)

    clerk.require_option_plan_taken(BUYS_ONE_CALL)
    assert clerk._symbol_to_register(BUYS_ONE_CALL) == "SPY"


async def test_with_the_setting_on_a_live_entry_passes_the_clerks_fence_and_is_judged_like_any_other(
    one_account: _OneAccount, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """Off, the entry is refused for the account, before anything else is asked. On, that refusal is gone:
    whatever the entry is then told, it is not that a Live account takes no option bot."""
    quote = _quote("1.90", "1.94", received_ms=NOON - 300)
    evidence = OptionEntryEvidence(
        picked={LEG: CALL}, multipliers={LEG: 100}, quotes={LEG: quote}, generation=GENERATION,
        rechecked_at_ms=NOON - 100, leg_gap_limit_percent=EIGHT, cost_limit_percent=TEN,
    )

    async def enter(decision: str):
        return await _live_clerk(one_account).execute_for_instance(
            strategy_instance_id="b", run_id="run-b", decision_id=decision, purpose=EffectPurpose.ENTER,
            action_plan=BUYS_ONE_CALL, quantity=1, option_entry=evidence,
        )

    asked_of_the_listing: list[str] = []

    async def listing(contract):
        asked_of_the_listing.append(contract.occ_symbol)
        return documented_option_listing(contract)

    monkeypatch.setattr(one_account.account, "get_option_contract", listing, raising=False)

    off = await enter("enter-off")
    monkeypatch.setattr(settings, _SETTING, True)
    on = await enter("enter-on")

    assert off.state is EffectOperationState.REJECTED
    assert off.explanation.startswith("OPTION_PLAN_ACCOUNT_NOT_OPEN: ") and _SETTING in off.explanation
    assert asked_of_the_listing == [CALL.occ_symbol], "only the entry the fence let through asked Alpaca's listing"
    assert "OPTION_PLAN_ACCOUNT_NOT_OPEN" not in (on.explanation or "")
    assert one_account.trade.submitted_legs == [], "an option entry is never sent as an order in shares"


async def test_a_live_shadow_takes_an_option_plan_whatever_the_setting_says(
    one_account: _OneAccount, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    for switched_on in (False, True):
        monkeypatch.setattr(settings, _SETTING, switched_on)
        _shadow_clerk(one_account).require_option_plan_taken(BUYS_ONE_CALL)
