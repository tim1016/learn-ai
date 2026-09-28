"""Whether Alpaca lets this account trade in its custody world (issue #1664).

One account observation decides at most one condition: the account read names
a different account, runs in the wrong mode for its world, is not active, or
has trading blocked. Home and the lane's attention bell show it as one account
line (``lane_summary._account_standing_items``), judged on the cached account
snapshot -- never a broker read of its own.

The Overview desk's operator card, which once rendered this beside the
custody-side recovery condition and an "Open Clerk recovery" shortcut, was
retired with the Trader/Operator lens (PRD #2560); nothing renders either
any more, so neither is authored.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.broker.contract.models import BrokerAccountSnapshot
from app.schemas.account_authority import (
    CustodyWorld,
    admitted_account_mode_for_world,
    world_admits_account_mode,
)

# ADR 0047: a failed custody authority is repaired by an offline ceremony, so
# the lane's attention line states the failure and never offers a cure.
AUTHORITY_FAILED_HEADLINE = "This account's custody authority has failed."


@dataclass(frozen=True)
class AccountEligibilityCondition:
    """The account's one standing problem: a stable id, how bad, and its copy."""

    condition_id: str
    severity: Literal["blocking", "warning"]
    headline: str
    detail: str


def account_eligibility_condition(
    account: BrokerAccountSnapshot, *, custody_world: CustodyWorld, identity_mismatch: bool,
) -> AccountEligibilityCondition | None:
    """The account's own eligibility condition, or ``None`` when Alpaca lets it trade.

    ``identity_mismatch`` means the observation named a different account than
    custody's: its mode, status and block flags then belong to that other
    account and are never judged as this one's (2026-08-20 review).
    """
    if identity_mismatch:
        return AccountEligibilityCondition(
            condition_id="alpaca_account_identity_mismatch",
            severity="blocking",
            headline="Account identity does not match this Clerk authority",
            detail=(
                "The Alpaca account observed by this read does not match the "
                "account this Clerk authority is bound to. Paper-mode and "
                "active-status checks cannot trust a different account's facts."
            ),
        )
    if not world_admits_account_mode(custody_world, account.account_mode):
        # The refusal names the world it is judging. A shadow or live authority
        # reads a real-money account, so telling its operator the account "is
        # not in paper mode" states the requirement backwards (design R8 #8).
        admitted = admitted_account_mode_for_world(custody_world)
        return AccountEligibilityCondition(
            condition_id="alpaca_account_wrong_execution_mode",
            severity="blocking",
            headline=f"This account is not in {admitted} mode",
            detail=(
                f"Deployment and manual orders on this authority require a {admitted}-mode "
                f"account; Alpaca reports {account.account_mode} mode for this account. "
                "This will not resolve by waiting — reconnect with the account this "
                "authority custodies, or activate the authority for this account."
            ),
        )
    if account.account_status.upper() != "ACTIVE":
        return AccountEligibilityCondition(
            condition_id="alpaca_account_inactive",
            severity="blocking",
            headline="This account is not active",
            detail=(
                f"Alpaca reports account status {account.account_status!r}; paper "
                "deployment and manual orders require an ACTIVE account."
            ),
        )
    if account.trading_blocked or account.account_blocked:
        return AccountEligibilityCondition(
            condition_id="alpaca_account_trading_blocked",
            severity="blocking",
            headline="Alpaca has blocked trading on this account",
            detail=(
                f"Alpaca reports trading_blocked={account.trading_blocked}, "
                f"account_blocked={account.account_blocked} for this account."
            ),
        )
    return None
