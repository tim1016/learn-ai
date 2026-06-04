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
        PoisonedHaltReason.from_json_dict({"trigger": PoisonedHaltTrigger.LOST_FILL.value, "details": {}})


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


# ──────────────────────────── check_outside_mutation ─────────────────


def test_outside_mutation_returns_none_when_all_executions_owned() -> None:
    from app.engine.live.halt import check_outside_mutation

    executions = [
        {"client_order_id": "live-1", "exec_id": "e1", "perm_id": 9001, "account_id": "DU123"},
        {"client_order_id": "live-2", "exec_id": "e2", "perm_id": 9002, "account_id": "DU123"},
    ]
    owned = {"live-1", "live-2"}
    assert check_outside_mutation(executions, owned, halted_at_ms=1_000, last_clean_bar_close_ms=900) is None


def test_outside_mutation_flags_first_foreign_execution() -> None:
    """A foreign execution under the DU account fires the halt regardless
    of clientId — § 7.1 trigger A."""
    from app.engine.live.halt import (
        PoisonedHaltTrigger,
        check_outside_mutation,
    )

    executions = [
        {"client_order_id": "live-1", "exec_id": "e1", "perm_id": 9001, "account_id": "DU123"},
        # Foreign — placed by a different client (e.g. TWS manual click).
        {
            "client_order_id": "manual-tws-7",
            "exec_id": "e2-foreign",
            "perm_id": 9002,
            "account_id": "DU123",
            "client_id": 0,
        },
    ]
    reason = check_outside_mutation(
        executions,
        owned_client_order_ids={"live-1"},
        halted_at_ms=1_000,
        last_clean_bar_close_ms=900,
    )
    assert reason is not None
    assert reason.trigger == PoisonedHaltTrigger.OUTSIDE_MUTATION
    assert reason.details["client_order_id"] == "manual-tws-7"
    assert reason.details["exec_id"] == "e2-foreign"
    assert reason.details["perm_id"] == 9002
    assert reason.details["client_id"] == 0


def test_outside_mutation_flags_execution_with_no_client_order_id() -> None:
    """An execution with a missing/null client_order_id is foreign by definition."""
    from app.engine.live.halt import check_outside_mutation

    executions = [
        {"client_order_id": None, "exec_id": "e-foreign", "perm_id": 1, "account_id": "DU123"},
    ]
    reason = check_outside_mutation(
        executions,
        owned_client_order_ids={"live-1"},
        halted_at_ms=1_000,
        last_clean_bar_close_ms=900,
    )
    assert reason is not None
    assert reason.details["client_order_id"] is None


def test_outside_mutation_ignores_foreign_execution_before_session_start() -> None:
    """A foreign execution whose broker time predates session start is
    pre-existing account history replayed at connect, not contamination.

    Regression: IBKR replays the trading day's prior executions when the
    runtime connects. A foreign fill from earlier in the session (before
    this run's broker session began) must not fatal-halt the bot — the bot
    had placed nothing yet, so it cannot own that fill, and the operator is
    expected to have reconciled pre-start state at deploy time. Without the
    ``session_start_ms`` floor this stale connect-time replay poisons the
    run (observed 2026-06-04: a clientId=42 fill at 09:30 ET halted a bot
    that started at 10:04 ET).
    """
    from app.engine.live.halt import check_outside_mutation

    executions = [
        {
            "client_order_id": None,
            "exec_id": "e-foreign-0930",
            "perm_id": 1176469133,
            "account_id": "DU284968",
            "client_id": 42,
            "exec_time_ms": 1_000,
        },
    ]
    reason = check_outside_mutation(
        executions,
        owned_client_order_ids={"live-1"},
        halted_at_ms=5_000,
        last_clean_bar_close_ms=4_900,
        session_start_ms=2_000,
    )
    assert reason is None


def test_outside_mutation_flags_foreign_execution_at_or_after_session_start() -> None:
    """A foreign execution at/after session start is concurrent contamination
    and still fatal-halts — the floor only suppresses provably-stale fills."""
    from app.engine.live.halt import PoisonedHaltTrigger, check_outside_mutation

    executions = [
        {
            "client_order_id": None,
            "exec_id": "e-foreign-live",
            "perm_id": 42,
            "account_id": "DU284968",
            "client_id": 99,
            "exec_time_ms": 2_500,
        },
    ]
    reason = check_outside_mutation(
        executions,
        owned_client_order_ids={"live-1"},
        halted_at_ms=5_000,
        last_clean_bar_close_ms=4_900,
        session_start_ms=2_000,
    )
    assert reason is not None
    assert reason.trigger == PoisonedHaltTrigger.OUTSIDE_MUTATION
    assert reason.details["exec_id"] == "e-foreign-live"


def test_outside_mutation_flags_foreign_execution_with_unknown_time_under_floor() -> None:
    """Fail-safe: a foreign execution with no broker time is still policed even
    when a session floor is set — the floor never suppresses a halt it cannot
    prove is stale."""
    from app.engine.live.halt import check_outside_mutation

    executions = [
        {
            "client_order_id": None,
            "exec_id": "e-foreign-notime",
            "perm_id": 7,
            "account_id": "DU284968",
            "exec_time_ms": None,
        },
    ]
    reason = check_outside_mutation(
        executions,
        owned_client_order_ids={"live-1"},
        halted_at_ms=5_000,
        last_clean_bar_close_ms=4_900,
        session_start_ms=2_000,
    )
    assert reason is not None


# ──────────────────────────── check_lost_fill ────────────────────────


def test_lost_fill_returns_none_when_all_orders_filled() -> None:
    from app.engine.live.halt import check_lost_fill

    orders = [{"client_order_id": "live-1", "submitted_at_ms": 100}]
    # remaining=0 marks the order complete. Any other value (or missing key)
    # leaves the order considered unfilled — see the partial-fill regression
    # test below.
    executions = [{"client_order_id": "live-1", "exec_id": "e1", "remaining": 0}]
    assert (
        check_lost_fill(
            orders,
            executions,
            fill_window_ms=60_000,
            current_time_ms=200,
            last_clean_bar_close_ms=0,
        )
        is None
    )


def test_lost_fill_flags_order_with_only_partial_execution_past_window() -> None:
    """A partial fill (remaining > 0) must NOT mark the order complete.

    Reviewer feedback (P2.1): the prior implementation considered an
    order filled iff ANY execution shared its client_order_id, so a
    1-share execution on a 200-share order would suppress the
    lost-fill halt indefinitely. After the fix, only executions with
    ``remaining == 0`` mark an order complete; everything else leaves
    the order eligible for the lost-fill halt when its window expires.
    """
    from app.engine.live.halt import PoisonedHaltTrigger, check_lost_fill

    orders = [{"client_order_id": "live-1", "submitted_at_ms": 100_000}]
    # A 1-share execution on a 200-share order: remaining=199. Order is
    # NOT complete. After fill_window_ms elapses, lost-fill must fire.
    executions = [{"client_order_id": "live-1", "exec_id": "e1", "remaining": 199}]
    reason = check_lost_fill(
        orders,
        executions,
        fill_window_ms=60_000,
        current_time_ms=200_000,
        last_clean_bar_close_ms=180_000,
    )
    assert reason is not None
    assert reason.trigger == PoisonedHaltTrigger.LOST_FILL
    assert reason.details["client_order_id"] == "live-1"


def test_lost_fill_returns_none_when_unfilled_order_still_within_window() -> None:
    """An order placed 30s ago with a 60s window is still hopeful — no halt yet."""
    from app.engine.live.halt import check_lost_fill

    orders = [{"client_order_id": "live-1", "submitted_at_ms": 100_000}]
    assert (
        check_lost_fill(
            orders,
            executions=[],
            fill_window_ms=60_000,
            current_time_ms=130_000,
            last_clean_bar_close_ms=100_000,
        )
        is None
    )


def test_lost_fill_flags_order_past_its_window() -> None:
    from app.engine.live.halt import (
        PoisonedHaltTrigger,
        check_lost_fill,
    )

    orders = [{"client_order_id": "live-1", "submitted_at_ms": 100_000}]
    reason = check_lost_fill(
        orders,
        executions=[],
        fill_window_ms=60_000,
        current_time_ms=200_000,
        last_clean_bar_close_ms=180_000,
    )
    assert reason is not None
    assert reason.trigger == PoisonedHaltTrigger.LOST_FILL
    assert reason.details["client_order_id"] == "live-1"
    assert reason.details["age_ms"] == 100_000
    assert reason.details["fill_window_ms"] == 60_000
    assert reason.halted_at_ms == 200_000
    assert reason.last_clean_bar_close_ms == 180_000


def test_lost_fill_reports_oldest_overdue_when_multiple() -> None:
    """When multiple orders are overdue, the oldest is reported first
    (the operator should chase that one's lifecycle)."""
    from app.engine.live.halt import check_lost_fill

    orders = [
        {"client_order_id": "live-2", "submitted_at_ms": 110_000},
        {"client_order_id": "live-1", "submitted_at_ms": 100_000},  # older
    ]
    reason = check_lost_fill(
        orders,
        executions=[],
        fill_window_ms=60_000,
        current_time_ms=200_000,
        last_clean_bar_close_ms=180_000,
    )
    assert reason is not None
    assert reason.details["client_order_id"] == "live-1"
    assert reason.details["overdue_count"] == 2


def test_lost_fill_skips_orders_with_no_client_order_id() -> None:
    """Internal/anonymous orders without a client_order_id are not
    Python-owned — the check is scoped to ownership, not all orders."""
    from app.engine.live.halt import check_lost_fill

    orders = [
        {"client_order_id": None, "submitted_at_ms": 100_000},
    ]
    assert (
        check_lost_fill(
            orders,
            executions=[],
            fill_window_ms=60_000,
            current_time_ms=200_000,
            last_clean_bar_close_ms=180_000,
        )
        is None
    )
