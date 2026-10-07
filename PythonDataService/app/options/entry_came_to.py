"""What one option entry came to, in the words a bot's runner logs and a Dry Run's journal writes (ADR 0076).

A closed set, spelled once. The runner works a word out from where the
account's Clerk says the entry stands (``bot_option_run.entry_came_to``); the
journal's row carries one (``bot_dry_run.DryRunActivity.entry_outcome``).

The runner is told the entry's standing and nothing finer, so each word
covers every case that standing covers:

- ``cancelled_unfilled`` is any order that reached the broker and ended with
  nothing filled: cancelled at its deadline, cancelled by a Stop, an EXIT or
  a closing Clerk, expired, or rejected by the broker after it had taken the
  order. The Clerk's order row keeps the broker's own last state for it; the
  standing does not, so the runner cannot tell these apart.
- ``rejected`` is an order that never rested: refused by the Clerk as it was
  sent, refused by the broker in its answer to the send, or proven never to
  have reached the broker.
- ``resting`` and ``unproven`` are the two ways an entry is not proven. The
  first is an order the books show at the broker. The second is an order the
  books cannot place: the broker's last answer was lost, or it counts a fill
  the books do not hold.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class EntryCameTo(StrEnum):
    """What one option entry came to. A closed set."""

    # Every contract asked for filled.
    FILLED = "filled"
    # It ended with some of it filled. The bot holds what filled.
    PART_FILLED = "part_filled_then_cancelled"
    # It reached the broker and ended with nothing filled, however it ended.
    CANCELLED_UNFILLED = "cancelled_unfilled"
    # It never rested: refused as it was sent, or proven never to have reached the broker.
    REJECTED = "rejected"
    # Not proven, and the books show its order at the broker: working, or asked to be cancelled.
    RESTING = "resting"
    # Not proven, and what the broker did with its order is not known.
    UNPROVEN = "unproven"


# The words for an entry that bought nothing. No fill's row carries one.
BOUGHT_NOTHING: Final = frozenset({EntryCameTo.CANCELLED_UNFILLED, EntryCameTo.REJECTED})


__all__ = ["BOUGHT_NOTHING", "EntryCameTo"]
