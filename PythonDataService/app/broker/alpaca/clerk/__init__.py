"""Activated SQLite Alpaca Account Clerk surface.

Pure custody arithmetic must import without composing the runtime import graph.
Keep the existing public functions, resolving their owner only when requested.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.active_authority import (
        get_alpaca_clerk,
        reset_alpaca_clerk_for_testing,
        set_alpaca_clerk,
    )

__all__ = [
    "get_alpaca_clerk",
    "reset_alpaca_clerk_for_testing",
    "set_alpaca_clerk",
]


def __getattr__(name: str) -> object:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from app.broker.alpaca.clerk import active_authority

    return getattr(active_authority, name)
