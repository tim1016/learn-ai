"""Run QuantLib pricing in an isolated interpreter with one active call (#3212).

AnyIO owns process startup, idle cleanup and event-loop shutdown. Cancellation
kills the active worker, including a C++ call that holds its interpreter lock.
Neither pricing route shares QuantLib's evaluation date with the live service.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

from anyio import CapacityLimiter, fail_after, to_process
from anyio.lowlevel import RunVar

_LIMITER: RunVar[CapacityLimiter] = RunVar("quantlib_pricing_limiter")
QUANTLIB_WAIT_S = 120.0


async def run_quantlib[T](price: Callable[..., T], /, **inputs: object) -> T:
    """Wait for one price without occupying an executor thread or the service's interpreter."""
    try:
        limiter = _LIMITER.get()
    except LookupError:
        limiter = CapacityLimiter(1)
        _LIMITER.set(limiter)
    try:
        with fail_after(QUANTLIB_WAIT_S) as bound:
            return await to_process.run_sync(partial(price, **inputs), cancellable=True, limiter=limiter)
    except TimeoutError as exc:
        if bound.cancel_called:
            raise TimeoutError(f"QuantLib pricing did not answer within {QUANTLIB_WAIT_S:g} seconds") from exc
        raise
