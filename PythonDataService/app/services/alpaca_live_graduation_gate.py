"""One process-wide fence shared by bot admission and live graduation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

_mutation_lock = asyncio.Lock()


@asynccontextmanager
async def graduation_mutation_fence() -> AsyncIterator[None]:
    """Serialize bot starts with the cutover's final observe-and-activate step."""

    async with _mutation_lock:
        yield

