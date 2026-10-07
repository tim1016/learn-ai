"""What this process's option bots ask to have watched.

Each option bot's runner says what it wants quoted: the window its plan's
rules can pick from, and the contracts it holds. The Clerk's IBKR supervisor
reads the whole list once a second and keeps its quote lines to it
(``app.broker.ibkr.option_quotes``, ADR 0076). A runner never opens a line of
its own.

The list is in memory and belongs to the process. A restart empties it, and
each runner says again what it wants when it starts; a contract a bot still
holds is asked for again by whoever runs or closes that bot.

A page asks on a lease. A bot's request stands until the bot takes it away.
A preview's stands only while the page keeps asking: each ask renews the
lease, and a request whose lease has run out is gone the next time the list
is read, so a page that went away stops holding quote lines.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.broker.ibkr.option_quotes import WatchRequest
from app.utils.timestamps import Clock, now_ms_utc


@dataclass(frozen=True, slots=True)
class _Lease:
    expires_ms: int
    # When this same request was first asked under its key. Renewing it unchanged keeps this.
    since_ms: int


class OptionWatchList:
    """One request for each asker, by the asker's own key. Asking again replaces what it asked before."""

    def __init__(self, clock: Clock = now_ms_utc) -> None:
        self._requests: dict[str, WatchRequest] = {}
        self._leases: dict[str, _Lease] = {}
        self._clock = clock

    def watch(self, key: str, request: WatchRequest) -> None:
        self._requests[key] = request
        self._leases.pop(key, None)

    def watch_on_lease(self, key: str, request: WatchRequest, *, lease_ms: int) -> int:
        """Ask for ``request`` to be watched for the next ``lease_ms``, and no longer unless asked again.

        Returns when this same request was first asked under ``key``: how
        long its quotes have had to arrive. A changed request starts again
        from now.
        """
        if lease_ms <= 0:
            raise ValueError(f"a lease lasts some time; got {lease_ms} ms")
        now = self._clock()
        self._drop_ended(now)
        held = self._leases.get(key)
        since_ms = held.since_ms if held is not None and self._requests[key] == request else now
        self._requests[key] = request
        self._leases[key] = _Lease(expires_ms=now + lease_ms, since_ms=since_ms)
        return since_ms

    def unwatch(self, key: str) -> None:
        self._requests.pop(key, None)
        self._leases.pop(key, None)

    def requests(self) -> tuple[WatchRequest, ...]:
        self._drop_ended(self._clock())
        return tuple(self._requests.values())

    def on_lease(self) -> tuple[str, ...]:
        """The keys asking on a lease now, the one whose lease ends soonest first."""
        self._drop_ended(self._clock())
        return tuple(sorted(self._leases, key=lambda key: self._leases[key].expires_ms))

    def _drop_ended(self, now: int) -> None:
        for key in [key for key, lease in self._leases.items() if now >= lease.expires_ms]:
            self.unwatch(key)


_watch_list = OptionWatchList()


def get_option_watch_list() -> OptionWatchList:
    """The list this process's IBKR supervisor reads."""
    return _watch_list
