"""Decision-table tests for the account's own eligibility (#1664).

Whether Alpaca lets the account trade in its custody world: an observation of
a different account, the wrong execution mode for the world, an inactive
account, and blocked trading each give one condition; an eligible account
gives none. Home and the bell show the condition as one account line.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.broker.alpaca.clerk.sqlite.account_eligibility import account_eligibility_condition
from app.broker.contract.models import BrokerAccountSnapshot
from app.schemas.account_authority import CustodyWorld


def _account(**overrides: Any) -> BrokerAccountSnapshot:
    base: dict[str, Any] = dict(
        broker="alpaca", account_id="PA1", account_mode="paper", account_status="ACTIVE", currency="USD",
        cash=100.0, equity=150.0, buying_power=300.0, portfolio_value=150.0, long_market_value=50.0,
        short_market_value=0.0, trading_blocked=False, account_blocked=False,
        created_at_ms=1_600_000_000_000, observed_at_ms=1_700_000_000_000,
    )
    base.update(overrides)
    return BrokerAccountSnapshot(**base)


def _judge(
    *, custody_world: CustodyWorld = "real_paper", identity_mismatch: bool = False, **account: Any,
):
    return account_eligibility_condition(
        _account(**account), custody_world=custody_world, identity_mismatch=identity_mismatch,
    )


def test_an_eligible_account_has_no_condition() -> None:
    assert _judge() is None


def test_inactive_account_blocks_with_its_status() -> None:
    condition = _judge(account_status="ACCOUNT_UPDATED")

    assert condition is not None
    assert condition.condition_id == "alpaca_account_inactive"
    assert condition.severity == "blocking"
    assert condition.headline == "This account is not active"


def test_shadow_custody_world_reads_a_live_account_without_the_wrong_mode_condition() -> None:
    """ADR 0059 D2: the shadow authority reads a live account by design.

    ``account_mode == "live"`` is only a misconfiguration in the real-paper
    world; the relaxation is scoped to the custody world, not to the mode.
    """
    assert _judge(account_mode="live", custody_world="shadow") is None
    real_paper = _judge(account_mode="live", custody_world="real_paper")
    assert real_paper is not None
    assert real_paper.condition_id == "alpaca_account_wrong_execution_mode"


@pytest.mark.parametrize(
    ("custody_world", "account_mode", "admitted"),
    [
        ("real_paper", "live", "paper"),
        ("shadow", "paper", "live"),
        ("real_live", "paper", "live"),
    ],
)
def test_the_wrong_mode_refusal_names_the_mode_its_own_world_admits(
    custody_world: CustodyWorld, account_mode: str, admitted: str
) -> None:
    """Design R8 #8: the copy names the world it is judging, not "paper" always.

    A static configuration problem no fresh evidence resolves, so it blocks.
    """
    condition = _judge(account_mode=account_mode, custody_world=custody_world)

    assert condition is not None
    assert condition.condition_id == "alpaca_account_wrong_execution_mode"
    assert condition.severity == "blocking"
    assert condition.headline == f"This account is not in {admitted} mode"


def test_account_identity_mismatch_outranks_every_fact_of_the_other_account() -> None:
    """A mismatched read's mode, status and block flags belong to that other
    account, so none of them is judged as this one's."""
    condition = _judge(identity_mismatch=True, account_mode="live", account_status="CLOSED", trading_blocked=True)

    assert condition is not None
    assert condition.condition_id == "alpaca_account_identity_mismatch"
    assert condition.severity == "blocking"
    assert condition.headline == "Alpaca reports a different account than this one"


@pytest.mark.parametrize("flags", [{"trading_blocked": True}, {"account_blocked": True}])
def test_blocked_trading_blocks(flags: dict[str, bool]) -> None:
    condition = _judge(**flags)

    assert condition is not None
    assert condition.condition_id == "alpaca_account_trading_blocked"
    assert condition.headline == "Alpaca has blocked trading on this account"


def test_the_wrong_mode_outranks_an_inactive_status() -> None:
    condition = _judge(account_mode="live", account_status="ACCOUNT_UPDATED")

    assert condition is not None
    assert condition.condition_id == "alpaca_account_wrong_execution_mode"
