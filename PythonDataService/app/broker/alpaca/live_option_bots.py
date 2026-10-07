"""Live accounts and option contracts: the one setting, and the rule every fence holds it to (ADR 0076 decision 24).

``ALPACA_LIVE_OPTION_BOTS_ENABLED`` opens Live accounts to bots that trade
option contracts. It is off unless a deployment turns it on, because no
option order from this system has been checked against Alpaca with real
money at stake. Three fences read it, each through this module: Deploy, the
account Clerk, and the Alpaca port.

**A fence against opening never stops a close.** Off, nothing that opens an
option position on a Live account is taken: no option plan is registered, no
ENTER is accepted, and no order with a leg that opens is sent. An order
whose every leg closes is taken as on any other day. A contract bought while
the setting was on is still held after it is switched off, and a restart
that loses the variable is an ordinary accident. Switching it off must never
leave a held contract with no way out: the bot's own EXIT, the Clerk's
scheduled close and Flatten can still reduce it. Stop does not itself
schedule a close. After a restart with the setting off, the option bot
cannot resume strategy monitoring; the Clerk and the owner can still close
what it holds.

The setting is read each time it is asked, never at import, so no fence
outlives a change of it.
"""

from __future__ import annotations

from typing import Final

from app.broker.contract.options import OptionOrderRequest, only_closes
from app.config import settings

SETTING: Final = "ALPACA_LIVE_OPTION_BOTS_ENABLED"


def live_option_bots_enabled() -> bool:
    """Whether this deployment has opened Live accounts to option bots. Off unless it says so."""
    return settings.ALPACA_LIVE_OPTION_BOTS_ENABLED


def live_refuses(order: OptionOrderRequest) -> bool:
    """Whether a Live account is sent no such order right now: the setting is off and a leg of it opens.

    Decided by the order's own legs. An order that only closes is never
    refused here, whatever the setting says.
    """
    return not live_option_bots_enabled() and not only_closes(order)


__all__ = ["SETTING", "live_option_bots_enabled", "live_refuses"]
