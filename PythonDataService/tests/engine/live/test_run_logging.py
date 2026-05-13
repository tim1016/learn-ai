"""Unit tests for ``app.engine.live.run_logging.configure_run_logging``."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from app.engine.live.run_logging import (
    DEFAULT_LOG_BACKUP_COUNT,
    DEFAULT_LOG_MAX_BYTES,
    configure_run_logging,
)


@pytest.fixture(autouse=True)
def _reset_root_logger() -> None:
    """Snapshot the root logger's handlers and restore them after each test.

    configure_run_logging mutates the root logger; without reset the
    handler list leaks between tests and a later test's assertions on
    handler counts become non-deterministic.
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    for handler in list(root.handlers):
        if handler not in saved_handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(saved_level)


def test_configure_run_logging_creates_log_file(tmp_path: Path) -> None:
    log_path = configure_run_logging(tmp_path / "run-001")
    assert log_path == tmp_path / "run-001" / "live.log"
    logger = logging.getLogger(__name__)
    logger.info("hello from the test")
    assert log_path.exists()
    contents = log_path.read_text(encoding="utf-8")
    assert "hello from the test" in contents


def test_configure_run_logging_rotation_caps_match_plan_spec(tmp_path: Path) -> None:
    log_path = configure_run_logging(tmp_path / "run-002")
    root = logging.getLogger()
    # Filter to *this* test's log path — earlier suite tests
    # (e.g. test_run_cli_shutdown) may have left their own
    # RotatingFileHandlers attached to the root logger. Asserting on
    # a global count was order-dependent; asserting on the handler we
    # just installed is what the test means.
    matching = [h for h in root.handlers if isinstance(h, RotatingFileHandler) and Path(h.baseFilename) == log_path]
    assert len(matching) == 1
    handler = matching[0]
    assert handler.maxBytes == DEFAULT_LOG_MAX_BYTES == 10 * 1024 * 1024
    assert handler.backupCount == DEFAULT_LOG_BACKUP_COUNT == 5


def test_configure_run_logging_step_prefix_renders_when_extra_passed(tmp_path: Path) -> None:
    log_path = configure_run_logging(tmp_path / "run-003")
    logger = logging.getLogger("test.step")
    logger.info("with step", extra={"step": "3"})
    logger.info("without step")
    contents = log_path.read_text(encoding="utf-8")
    assert "[STEP 3] with step" in contents
    # No bare "[STEP " marker should land on the without-step record.
    line_without_step = next(line for line in contents.splitlines() if "without step" in line)
    assert "[STEP" not in line_without_step


def test_configure_run_logging_idempotent_does_not_duplicate_file_handlers(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-004"
    configure_run_logging(run_dir)
    configure_run_logging(run_dir)
    root = logging.getLogger()
    matching = [
        h for h in root.handlers if isinstance(h, RotatingFileHandler) and Path(h.baseFilename) == run_dir / "live.log"
    ]
    assert len(matching) == 1


def test_configure_run_logging_creates_run_dir_if_missing(tmp_path: Path) -> None:
    run_dir = tmp_path / "deep" / "nested" / "run-005"
    assert not run_dir.exists()
    log_path = configure_run_logging(run_dir)
    assert run_dir.is_dir()
    assert log_path == run_dir / "live.log"
