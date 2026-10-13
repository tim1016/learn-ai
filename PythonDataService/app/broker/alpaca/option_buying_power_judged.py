"""Whether an option entry's comparison with Alpaca's option buying power is judged: the one setting (ADR 0076 decision 26).

``ALPACA_OPTION_BUYING_POWER_JUDGED`` says that Alpaca has been seen, on the
account, to move its option buying power by an order's cost. Off, both
readings, the fall and what was set aside are recorded for every option
entry on a real account, each comparison reads "unknown: not switched on",
and no bot is ever held. On, a clean pair of readings is judged, and a fall
of more than the bot set aside holds that bot's new entries until the owner
has reviewed it. It is off unless a deployment turns it on.

Two places ask it: the Clerk's comparison, which acts on the answer, and the
route that serves a bot's comparisons, which says what the account does. Each
asks here.

The setting is read each time it is asked, never at import, so no judgment
outlives a change of it.
"""

from __future__ import annotations

from typing import Final

from app.config import settings

SETTING: Final = "ALPACA_OPTION_BUYING_POWER_JUDGED"


def option_buying_power_judged() -> bool:
    """Whether a clean pair of an option entry's readings is judged on this deployment. Off unless it says so."""
    return settings.ALPACA_OPTION_BUYING_POWER_JUDGED


__all__ = ["SETTING", "option_buying_power_judged"]
