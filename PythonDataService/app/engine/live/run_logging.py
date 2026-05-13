"""File + console logging configuration for ``run.py start``.

Splits a single helper out of ``run.py`` for two reasons:
  * Keeps ``run.py`` focused on subcommand dispatch.
  * Lets the helper be unit-tested directly without spinning up the
    live engine.

Format includes a ``[STEP X]`` marker when callers pass
``extra={"step": "N"}`` (e.g. ``logger.info("...", extra={"step": "3"})``).
For callers that don't pass ``step``, the marker is omitted via a
custom filter that defaults the attribute to an empty string and a
conditional format string. This matches the same-shape format
``ibkr-integration-authority.md`` § 6 recommends for live runs.

Rotation: ``RotatingFileHandler`` with ``maxBytes=10*1024*1024`` and
``backupCount=5`` per the deployment plan §10. At default INFO level
a full RTH session writes a few hundred lines (1-2 KB), so the cap
covers ~5000 sessions before the oldest rotation drops. Size-based
(not time-based) is the explicit decision recorded in the IBKR
hardening plan.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_LOG_BACKUP_COUNT = 5


class _StepDefaultFilter(logging.Filter):
    """Ensure every record carries a ``step`` attribute (default empty).

    Without this, formatting a record that didn't pass
    ``extra={"step": ...}`` would raise ``KeyError`` against the
    ``%(step)s`` placeholder. The filter is attached to both handlers
    so console and file output share the same shape.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "step"):
            record.step = ""
        return True


def _format_step(record: logging.LogRecord) -> str:
    step = getattr(record, "step", "")
    return f"[STEP {step}] " if step else ""


class _StepFormatter(logging.Formatter):
    """Formatter that inlines a ``[STEP X]`` prefix when present."""

    def format(self, record: logging.LogRecord) -> str:
        record.step_prefix = _format_step(record)
        return super().format(record)


_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(step_prefix)s%(message)s"


def configure_run_logging(
    run_dir: Path,
    *,
    level: int = logging.INFO,
    max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
    log_filename: str = "live.log",
) -> Path:
    """Attach a file + console handler pair to the root logger.

    Idempotent: calling it twice on the same ``run_dir`` does not
    duplicate handlers (it removes any previously-installed handlers
    that target the same file before adding the new pair). Returns
    the resolved log-file path so callers can echo it to the operator.

    ``run_dir`` is created if missing — keeps ``cmd_start`` from
    having to ``mkdir`` separately.
    """
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / log_filename

    root = logging.getLogger()
    root.setLevel(level)

    # Drop any prior handlers that target this exact log path so a
    # second configure_run_logging call (e.g. in a test reusing the
    # tmp_path fixture) doesn't double-write every line.
    for handler in list(root.handlers):
        if isinstance(handler, RotatingFileHandler) and Path(handler.baseFilename) == log_path:
            root.removeHandler(handler)
            handler.close()

    formatter = _StepFormatter(_LOG_FORMAT)
    step_filter = _StepDefaultFilter()

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(step_filter)
    root.addHandler(file_handler)

    # Console handler is only added once across the process — multiple
    # cmd_start invocations in the same process (unusual outside tests)
    # would otherwise stack console output. Keyed by a sentinel
    # attribute on the handler.
    if not any(getattr(h, "_run_logging_console", False) for h in root.handlers):
        console_handler = logging.StreamHandler()
        console_handler.setLevel(level)
        console_handler.setFormatter(formatter)
        console_handler.addFilter(step_filter)
        console_handler._run_logging_console = True  # type: ignore[attr-defined]
        root.addHandler(console_handler)

    return log_path
