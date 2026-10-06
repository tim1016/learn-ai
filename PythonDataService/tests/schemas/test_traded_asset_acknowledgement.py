"""The one rule for the owner's acknowledgement of an untested traded asset (#2912)."""

from __future__ import annotations

import pytest

from app.schemas.action_plan import ActionPlan
from app.schemas.traded_asset_acknowledgement import (
    UntestedTradedAssetAcknowledgement,
    traded_asset_acknowledgement_fault,
)
from app.services.bot_binding_repository import alpaca_v1_action_plan

_GIVEN = UntestedTradedAssetAcknowledgement(
    acknowledgement="I_ACCEPT_UNTESTED_TRADED_ASSET_RISK", reason="QQQ has followed SPY closely enough to try this.",
)
_CALLS_ON_SPY = ActionPlan.model_validate({
    "on_enter": [{
        "leg_id": "primary", "instrument": {"kind": "option", "underlying": "SPY"}, "position": "long",
        "qty_ratio": 1, "right": "call", "strike": {"selector": "atm"}, "expiry": {"selector": "nearest_weekly"},
    }],
    "on_exit": [{"kind": "close_leg", "leg_id": "close", "entry_leg_id": "primary"}],
})


@pytest.mark.parametrize(
    ("mode", "plan", "given", "fault"),
    [
        ("trade", alpaca_v1_action_plan("QQQ"), None, "required"),
        ("trade", alpaca_v1_action_plan("QQQ"), _GIVEN, None),
        ("trade", alpaca_v1_action_plan("SPY"), None, None),
        ("trade", alpaca_v1_action_plan("SPY"), _GIVEN, "not_applicable"),
        ("dry_run", alpaca_v1_action_plan("QQQ"), None, None),
        ("dry_run", alpaca_v1_action_plan("QQQ"), _GIVEN, "not_applicable"),
        ("log_only", alpaca_v1_action_plan("QQQ"), None, None),
        # Options on the symbol the bot listens to are not its shares: the
        # tickers match, and the bot still trades something else.
        ("trade", _CALLS_ON_SPY, None, "required"),
        ("trade", _CALLS_ON_SPY, _GIVEN, None),
    ],
)
def test_who_needs_an_acknowledgement_and_who_may_not_carry_one(
    mode: str, plan: ActionPlan, given: UntestedTradedAssetAcknowledgement | None, fault: str | None
) -> None:
    """A bot listening to SPY: asked only when it trades the account's money in something else."""
    assert traded_asset_acknowledgement_fault(
        mode=mode, action_plan=plan, signal_symbol="SPY", acknowledgement=given,  # type: ignore[arg-type]
    ) == fault
