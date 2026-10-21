"""One option request budget, foreground before bounded archive waiters."""
from __future__ import annotations

import asyncio
from collections import deque
from contextvars import ContextVar

archive_request: ContextVar[bool] = ContextVar("archive_option_request", default=False)


class OptionRequestGate:
    def __init__(self, value: int) -> None:
        self._value = value
        self._waiters: deque[tuple[asyncio.Future[None], bool]] = deque()

    def locked(self) -> bool:
        # A granted waiter that has not resumed yet already holds its slot; only pending ones queue new callers.
        return self._value == 0 or any(not waiter.done() for waiter, _ in self._waiters)

    async def acquire(self) -> bool:
        if not self.locked():
            self._value -= 1
            return True
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        entry = (waiter, archive_request.get())
        self._waiters.append(entry)
        try:
            await waiter
        except asyncio.CancelledError:
            if not waiter.cancelled():
                self.release()
            raise
        finally:
            self._waiters.remove(entry)
        return True

    def release(self) -> None:
        available = [(future, background) for future, background in self._waiters if not future.done()]
        if available:
            foreground = next((future for future, background in available if not background), available[0][0])
            foreground.set_result(None)
        else:
            self._value += 1

    async def __aenter__(self) -> None:
        await self.acquire()

    async def __aexit__(self, *args: object) -> None:
        self.release()
