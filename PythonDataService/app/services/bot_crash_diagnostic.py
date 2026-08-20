"""Capture bounded, immutable diagnostics for supervised bot crashes."""

from __future__ import annotations

import traceback
from pathlib import Path

from app.schemas.bot_run_evidence import (
    CRASH_EXCEPTION_TYPE_MAX_LENGTH,
    CRASH_MESSAGE_MAX_LENGTH,
    CRASH_SOURCE_FILE_MAX_LENGTH,
    BotCrashDiagnostic,
)

_SERVICE_ROOT = Path(__file__).resolve().parents[2]


def _bounded(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return f"{value[: max_length - 1]}…"


def _safe_message(error: Exception) -> str:
    try:
        return str(error) or "Exception raised without a message."
    except Exception:
        return "Exception message could not be rendered."


def capture_bot_crash_diagnostic(error: Exception) -> BotCrashDiagnostic:
    """Capture the final traceback frame without letting evidence exceed its schema."""
    frame = traceback.extract_tb(error.__traceback__)[-1]
    source_path = Path(frame.filename)
    try:
        source_file = source_path.resolve().relative_to(_SERVICE_ROOT).as_posix()
    except (OSError, ValueError):
        source_file = source_path.as_posix()
    return BotCrashDiagnostic(
        exception_type=_bounded(type(error).__name__, CRASH_EXCEPTION_TYPE_MAX_LENGTH),
        message=_bounded(_safe_message(error), CRASH_MESSAGE_MAX_LENGTH),
        source_file=_bounded(source_file, CRASH_SOURCE_FILE_MAX_LENGTH),
        source_line=frame.lineno,
    )


__all__ = ["capture_bot_crash_diagnostic"]
