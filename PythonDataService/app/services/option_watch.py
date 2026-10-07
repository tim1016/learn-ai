"""What this process's option bots ask to have watched.

Each option bot's runner says what it wants quoted: the window its plan's
rules can pick from, and the contracts it holds. The Clerk's IBKR supervisor
reads the whole list once a second and keeps its quote lines to it
(``app.broker.ibkr.option_quotes``, ADR 0076). A runner never opens a line of
its own.

The list is in memory and belongs to the process. A restart empties it, and
each runner says again what it wants when it starts; a contract a bot still
holds is asked for again by whoever runs or closes that bot.
"""

from __future__ import annotations

from app.broker.ibkr.option_quotes import WatchRequest


class OptionWatchList:
    """One request for each asker, by the asker's own key. Asking again replaces what it asked before."""

    def __init__(self) -> None:
        self._requests: dict[str, WatchRequest] = {}

    def watch(self, key: str, request: WatchRequest) -> None:
        self._requests[key] = request

    def unwatch(self, key: str) -> None:
        self._requests.pop(key, None)

    def requests(self) -> tuple[WatchRequest, ...]:
        return tuple(self._requests.values())


_watch_list = OptionWatchList()


def get_option_watch_list() -> OptionWatchList:
    """The list this process's IBKR supervisor reads."""
    return _watch_list
