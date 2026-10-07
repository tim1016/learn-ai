"""What a search worker that does not come up as it should runs when it starts.

A spawned process finds what it runs by name, so these live in a module of
their own, and one that loads nothing: the process is at this in milliseconds.
"""

from __future__ import annotations

import time


def never_comes_up() -> None:
    """A worker whose start never finishes."""
    while True:
        time.sleep(60)
