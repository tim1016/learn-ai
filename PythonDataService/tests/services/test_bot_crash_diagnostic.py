"""Tests for immutable supervised-bot crash evidence."""

from __future__ import annotations

from app.schemas.bot_run_evidence import CRASH_MESSAGE_MAX_LENGTH
from app.services.bot_crash_diagnostic import capture_bot_crash_diagnostic


def _capture(error: Exception):
    try:
        raise error
    except Exception as caught:
        return capture_bot_crash_diagnostic(caught)


def test_capture_preserves_exact_type_message_and_final_frame() -> None:
    message = "TradeBar.__init__() got an unexpected keyword argument 'start_ms'"

    diagnostic = _capture(TypeError(message))

    assert diagnostic.exception_type == "TypeError"
    assert diagnostic.message == message
    assert diagnostic.source_file.endswith("tests/services/test_bot_crash_diagnostic.py")
    assert diagnostic.source_line > 0


def test_capture_bounds_long_messages_without_losing_terminal_evidence() -> None:
    diagnostic = _capture(RuntimeError("x" * (CRASH_MESSAGE_MAX_LENGTH + 100)))

    assert len(diagnostic.message) == CRASH_MESSAGE_MAX_LENGTH
    assert diagnostic.message.endswith("…")


def test_capture_survives_an_exception_with_an_unrenderable_message() -> None:
    class UnrenderableError(Exception):
        def __str__(self) -> str:
            raise RuntimeError("message rendering failed")

    diagnostic = _capture(UnrenderableError())

    assert diagnostic.exception_type == "UnrenderableError"
    assert diagnostic.message == "Exception message could not be rendered."
