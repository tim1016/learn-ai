"""A real account and an option order of several legs: the one setting (ADR 0076).

``ALPACA_MULTI_LEG_OPTION_ORDERS_ENABLED`` opens a Paper and a Live account
to a bot whose structure has several legs. It is off unless a deployment
turns it on. An order of several legs is one order at Alpaca with an order
of its own for each leg. Paper recordings cover a debit spread; the remaining
qualification steps still govern promotion. A leg read wrongly could be taken
for an order nobody placed, which holds every bot on the account. A real
account refuses such a plan by name while this deployment's setting is off.

A Dry Run and a Live Shadow never read it: a simulation sends nothing. A
plan of one leg never reads it either.

The setting is read each time it is asked, never at import, so no refusal
outlives a change of it.
"""

from __future__ import annotations

from typing import Final

from app.config import settings

SETTING: Final = "ALPACA_MULTI_LEG_OPTION_ORDERS_ENABLED"


def multi_leg_option_orders_enabled() -> bool:
    """Whether this deployment has opened real accounts to option orders of several legs. Off unless it says so."""
    return settings.ALPACA_MULTI_LEG_OPTION_ORDERS_ENABLED


__all__ = ["SETTING", "multi_leg_option_orders_enabled"]
