"""Strategy base class and algorithm implementations.

The re-exports stay lazy (#2450): importing ``base`` or ``signal_intent``
eagerly here would import declared Signal Program sources before the
build-proof source anchor (``app.services.program_source_anchor``, run at
the top of ``app.main``) has hashed them, and the anchor then refuses to
boot. Every existing consumer imports the submodules directly
(``from app.engine.strategy.base import Strategy``); the attribute access
below keeps that same surface working without the eager import.
"""

import importlib

__all__ = ["SignalIntent", "SignalIntentKind", "Strategy", "StrategyContext"]

_RE_EXPORTS = {
    "Strategy": "app.engine.strategy.base",
    "StrategyContext": "app.engine.strategy.base",
    "SignalIntent": "app.engine.strategy.signal_intent",
    "SignalIntentKind": "app.engine.strategy.signal_intent",
}


def __getattr__(name: str):
    module_name = _RE_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(module_name), name)
