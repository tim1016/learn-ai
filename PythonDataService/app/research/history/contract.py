"""What a store must give the Research History, and what a request asks of it.

A source reads one store and nothing else. It is asked for the first ``take`` records matching the query, in the one
order every source shares, and for how many match in all. Two sources never need to agree on anything but that order,
which is why the merge can page the union exactly instead of concatenating truncated lists.

The order is: newest ``created_at_ms`` first, then ``type``, then the native id. A native id compares in the store's own
way (a number when the store counts, text when it hashes), which is safe because ``type`` always decides before two
ids from different stores could be compared.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.schemas.research_history import HistoryStatus, HistoryType, ResearchHistoryEntry


@dataclass(frozen=True, slots=True)
class HistoryQuery:
    """The filters of a request. ``None`` is no filter."""

    symbol: str | None = None
    status: HistoryStatus | None = None
    created_from_ms: int | None = None
    created_before_ms: int | None = None

    def admits_time(self, created_at_ms: int) -> bool:
        if self.created_from_ms is not None and created_at_ms < self.created_from_ms:
            return False
        return self.created_before_ms is None or created_at_ms < self.created_before_ms


@dataclass(frozen=True, slots=True)
class SourceRead:
    """One store's answer to a query."""

    total: int
    """How many records match in this store."""
    entries: tuple[ResearchHistoryEntry, ...]
    """The first ``take`` matches, or all of them when fewer, in the shared order."""
    symbols: frozenset[str]
    """Every symbol the store holds within the time filter, whatever the symbol and status filters say."""
    unreadable: int = 0
    """Records the store holds that could not be read: in neither ``total`` nor ``entries``."""


class SourceUnavailableError(Exception):
    """A store that cannot be read right now. The message is the reason, in words the page can show."""


class HistorySource(Protocol):
    """One store, registered once."""

    type: HistoryType
    label: str
    """The store in a phrase, for a sentence about it: "saved walk-forward runs"."""

    def order_key(self, native_id: str) -> int | str:
        """The id as this store orders it."""
        ...

    async def read(self, query: HistoryQuery, take: int) -> SourceRead:
        """Raise ``SourceUnavailableError`` when the store cannot be read; never answer a failure as an empty store."""
        ...
