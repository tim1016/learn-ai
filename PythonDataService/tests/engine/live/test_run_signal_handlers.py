"""Unit tests for ``_install_signal_handlers`` in ``app.engine.live.run``.

The end-to-end signal → shutdown_event → engine flatten path is covered
by the FakeBroker shutdown integration test; these tests verify the
wiring shape (SIGINT + SIGTERM both registered; Windows graceful
fallback) without sending real signals.
"""

from __future__ import annotations

import asyncio
import signal
from unittest.mock import MagicMock

from app.engine.live.run import _install_signal_handlers


def test_install_signal_handlers_registers_sigint_and_sigterm() -> None:
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    event = asyncio.Event()
    _install_signal_handlers(loop, event)
    registered_sigs = {call.args[0] for call in loop.add_signal_handler.call_args_list}
    assert registered_sigs == {signal.SIGINT, signal.SIGTERM}


def test_install_signal_handlers_handler_sets_shutdown_event() -> None:
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    event = asyncio.Event()
    _install_signal_handlers(loop, event)
    # Pull the handler the helper registered for SIGINT and invoke it
    # directly with the same sig_value the loop would pass through.
    sigint_call = next(call for call in loop.add_signal_handler.call_args_list if call.args[0] == signal.SIGINT)
    handler = sigint_call.args[1]
    sig_value = sigint_call.args[2]
    assert not event.is_set()
    handler(sig_value)
    assert event.is_set()


def test_install_signal_handlers_swallows_notimplementederror_on_windows() -> None:
    """On Windows event loops add_signal_handler raises NotImplementedError.

    The helper logs a warning and continues — operator runs on Windows
    fall through to the generic exception path on Ctrl-C without
    graceful flatten.
    """
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    loop.add_signal_handler.side_effect = NotImplementedError
    event = asyncio.Event()
    # Should not raise.
    _install_signal_handlers(loop, event)
    # Both signals attempted (helper logs once per signal that fails).
    assert loop.add_signal_handler.call_count == 2
