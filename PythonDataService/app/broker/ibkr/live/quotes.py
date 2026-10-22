"""Typed callback receipts and failures, independent of the delivery adapter."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from eventkit import Event

from app.broker.ibkr.live.protocol import PRICE_TICKS, QuoteUpdate


@dataclass(frozen=True)
class QuoteFailure:
    reason: str
    code: int | None = None


class QuoteLease:
    """A consumer owns future receipts and a release, never a vendor ticker.

    No initial snapshot is replayed. Every update carries its original receipt;
    a failure withdraws the latest snapshot before notifying the consumer.
    """

    def __init__(self, release: Callable[[], None]) -> None:
        self.latest: QuoteUpdate | None = None
        self.updates = Event()
        self.failures = Event()
        self._release: Callable[[], None] | None = release

    def publish(self, update: QuoteUpdate) -> None:
        values = dict(update.values)
        tick_kinds = {tick.kind for tick in update.ticks}
        for name, kinds in PRICE_TICKS.items():
            value = values.get(name)
            if value is not None and value > 0 and not kinds & tick_kinds:
                values[name] = None if self.latest is None else self.latest.values.get(name)
        update = update.model_copy(update={"values": values})
        self.latest = update
        self.updates.emit(update)

    def fail(self, failure: QuoteFailure) -> None:
        self.latest = None
        self.failures.emit(failure)

    def release(self) -> None:
        if self._release is not None:
            release, self._release = self._release, None
            release()
            self.latest = None
