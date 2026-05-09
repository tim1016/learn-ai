"""Tests for app.engine.live.halt — poisoned.flag I/O.

Locks the on-disk format so subsequent PRs (cmd_start refusal,
LiveEngine detection wiring, emergency-flatten) read/write the same
shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.engine.live.halt import (
    POISONED_FLAG_FILENAME,
    PoisonedHaltReason,
    PoisonedHaltTrigger,
    is_run_poisoned,
    now_ms_utc,
    read_poisoned_flag,
    write_poisoned_flag,
)

# ──────────────────────────── PoisonedHaltReason ─────────────────────


def test_poisoned_halt_reason_round_trips_through_json() -> None:
    reason = PoisonedHaltReason(
        trigger=PoisonedHaltTrigger.OUTSIDE_MUTATION,
        halted_at_ms=1_700_000_000_500,
        last_clean_bar_close_ms=1_700_000_000_000,
        details={"exec_id": "exec-foreign-1", "perm_id": 9001, "client_id": 0},
    )
    payload = reason.to_json_dict()
    rebuilt = PoisonedHaltReason.from_json_dict(payload)
    assert rebuilt == reason


def test_poisoned_halt_reason_rejects_invalid_trigger() -> None:
    with pytest.raises(ValueError, match="trigger"):
        PoisonedHaltReason.from_json_dict(
            {
                "trigger": "made_up_trigger",
                "halted_at_ms": 1,
                "last_clean_bar_close_ms": 0,
                "details": {},
            }
        )


def test_poisoned_halt_reason_rejects_missing_timestamps() -> None:
    with pytest.raises(ValueError, match="timestamp"):
        PoisonedHaltReason.from_json_dict(
            {"trigger": PoisonedHaltTrigger.LOST_FILL.value, "details": {}}
        )


def test_poisoned_halt_reason_rejects_non_dict_details() -> None:
    with pytest.raises(ValueError, match="details"):
        PoisonedHaltReason.from_json_dict(
            {
                "trigger": PoisonedHaltTrigger.LOST_FILL.value,
                "halted_at_ms": 1,
                "last_clean_bar_close_ms": 0,
                "details": "not-a-dict",
            }
        )


# ──────────────────────────── write/read round-trip ──────────────────


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    reason = PoisonedHaltReason(
        trigger=PoisonedHaltTrigger.LOST_FILL,
        halted_at_ms=1_700_000_010_000,
        last_clean_bar_close_ms=1_700_000_000_000,
        details={"client_order_id": "live-42", "expected_fill_window_ms": 60_000},
    )
    path = write_poisoned_flag(tmp_path, reason)
    assert path == tmp_path / POISONED_FLAG_FILENAME
    assert path.exists()

    rebuilt = read_poisoned_flag(tmp_path)
    assert rebuilt == reason


def test_read_returns_none_when_no_flag(tmp_path: Path) -> None:
    assert read_poisoned_flag(tmp_path) is None
    assert is_run_poisoned(tmp_path) is False


def test_is_run_poisoned_true_after_write(tmp_path: Path) -> None:
    write_poisoned_flag(
        tmp_path,
        PoisonedHaltReason(
            trigger=PoisonedHaltTrigger.OUTSIDE_MUTATION,
            halted_at_ms=1,
            last_clean_bar_close_ms=0,
        ),
    )
    assert is_run_poisoned(tmp_path) is True


def test_write_refuses_to_overwrite_existing_flag(tmp_path: Path) -> None:
    """First halt wins — a second halt on the same run can't silently
    rewrite the cause. The operator needs to investigate the original."""
    first = PoisonedHaltReason(
        trigger=PoisonedHaltTrigger.OUTSIDE_MUTATION,
        halted_at_ms=1_000,
        last_clean_bar_close_ms=0,
    )
    second = PoisonedHaltReason(
        trigger=PoisonedHaltTrigger.LOST_FILL,
        halted_at_ms=2_000,
        last_clean_bar_close_ms=0,
    )
    write_poisoned_flag(tmp_path, first)
    with pytest.raises(FileExistsError):
        write_poisoned_flag(tmp_path, second)
    # On-disk content is the original, not the second attempt.
    assert read_poisoned_flag(tmp_path) == first


def test_read_raises_on_corrupted_flag(tmp_path: Path) -> None:
    """A corrupted flag must NOT silently behave like 'no flag' — that
    would let a contaminated run resume."""
    (tmp_path / POISONED_FLAG_FILENAME).write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError, match="unreadable"):
        read_poisoned_flag(tmp_path)


def test_read_raises_when_payload_is_not_object(tmp_path: Path) -> None:
    (tmp_path / POISONED_FLAG_FILENAME).write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        read_poisoned_flag(tmp_path)


def test_write_creates_run_dir_if_missing(tmp_path: Path) -> None:
    target = tmp_path / "live_runs" / "abc123"
    write_poisoned_flag(
        target,
        PoisonedHaltReason(
            trigger=PoisonedHaltTrigger.OUTSIDE_MUTATION,
            halted_at_ms=1,
            last_clean_bar_close_ms=0,
        ),
    )
    assert (target / POISONED_FLAG_FILENAME).exists()


# ──────────────────────────── now_ms_utc ─────────────────────────────


def test_now_ms_utc_returns_int_milliseconds() -> None:
    ts = now_ms_utc()
    assert isinstance(ts, int)
    # Sanity: post-2020 UTC ms is 13 digits.
    assert ts > 1_577_836_800_000  # 2020-01-01 UTC
