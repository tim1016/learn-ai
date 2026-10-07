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

Pages may be held to a number of leases. A page that asks when they are all
taken is given the one asked for longest ago, which is most likely a page
that has gone. The page that gave way is remembered: while it keeps asking
and every lease is still taken, it is told so and takes nobody else's, so the
same pages do not turn each other out ask after ask.
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
        # The pages that gave their lease up to a newer one, and until when that is remembered of each.
        self._gave_way: dict[str, int] = {}
        self._clock = clock

    def watch(self, key: str, request: WatchRequest) -> None:
        self._requests[key] = request
        self._leases.pop(key, None)

    def watch_on_lease(self, key: str, request: WatchRequest, *, lease_ms: int, at_most: int | None = None) -> int | None:
        """Ask for ``request`` to be watched for the next ``lease_ms``, and no longer unless asked again.

        Returns when this same request was first asked under ``key``: how
        long its quotes have had to arrive. A changed request starts again
        from now.

        ``at_most`` is the most leases there may be. A key that asks for one
        more takes the lease asked for longest ago. Returns None, and watches
        nothing, for a key that gave its own lease up that way and asks again
        while they are all still taken.
        """
        if lease_ms <= 0:
            raise ValueError(f"a lease lasts some time; got {lease_ms} ms")
        if at_most is not None and at_most < 1:
            raise ValueError(f"at least one lease may be held; got {at_most}")
        now = self._clock()
        self._drop_ended(now)
        held = self._leases.get(key)
        if held is None and at_most is not None and len(self._leases) >= at_most:
            if key in self._gave_way:
                self._gave_way[key] = now + lease_ms
                return None
            for longest_ago in self.on_lease()[: len(self._leases) - (at_most - 1)]:
                self.unwatch(longest_ago)
                self._gave_way[longest_ago] = now + lease_ms
        self._gave_way.pop(key, None)
        since_ms = held.since_ms if held is not None and self._requests[key] == request else now
        self._requests[key] = request
        self._leases[key] = _Lease(expires_ms=now + lease_ms, since_ms=since_ms)
        return since_ms

    def unwatch(self, key: str) -> None:
        self._requests.pop(key, None)
        self._leases.pop(key, None)
        self._gave_way.pop(key, None)

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
        for key in [key for key, until_ms in self._gave_way.items() if now >= until_ms]:
            del self._gave_way[key]


_watch_list = OptionWatchList()


def get_option_watch_list() -> OptionWatchList:
    """The list this process's IBKR supervisor reads."""
    return _watch_list
