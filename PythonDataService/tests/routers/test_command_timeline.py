"""Tests for the canonical command timeline builder (#397)."""

from __future__ import annotations

import json
from pathlib import Path

from app.routers.live_instances import _resolve_durable_control_write_failure
from app.routers.live_runs import COMMAND_POLL_INTERVAL_MS, build_command_timeline


def _pending(d: Path, seq: int, verb: str, payload: dict | None = None) -> None:
    (d / f"command.{seq}.{verb}.pending.json").write_text(
        json.dumps({"seq": seq, "verb": verb, "payload": payload or {}}), encoding="utf-8"
    )


def _ack(d: Path, seq: int, verb: str, outcome: dict) -> None:
    (d / f"command.{seq}.{verb}.ack.json").write_text(
        json.dumps({"seq": seq, "verb": verb, "outcome": outcome}), encoding="utf-8"
    )


def test_timeline_unifies_queued_acknowledged_failed(tmp_path: Path) -> None:
    d = tmp_path / "commands"
    d.mkdir()
    _pending(d, 1, "RECONCILE", {"issued_by": "op", "reason": "manual"})  # queued
    _pending(d, 2, "FLATTEN")
    _ack(d, 2, "FLATTEN", {"status": "ok", "effect": "flat"})  # acknowledged
    _pending(d, 3, "MARK_POISONED")
    _ack(
        d,
        3,
        "MARK_POISONED",
        {
            "status": "error",
            "reason_code": "DURABLE_CONTROL_WRITE_FAILED",
            "effect": "boom",
        },
    )  # failed

    timeline = build_command_timeline(d)

    assert timeline.poll_interval_ms == COMMAND_POLL_INTERVAL_MS
    assert [e.seq for e in timeline.entries] == [3, 2, 1]  # newest first, one entry per command
    by_seq = {e.seq: e for e in timeline.entries}

    assert by_seq[1].status == "queued"
    assert by_seq[1].issued_by == "op"
    assert by_seq[1].reason == "manual"
    assert by_seq[1].queued_at_ms is not None
    assert by_seq[1].acked_at_ms is None

    assert by_seq[2].status == "acknowledged"
    assert by_seq[2].outcome == "ok"
    assert by_seq[2].outcome_detail == "flat"
    assert by_seq[2].acked_at_ms is not None

    assert by_seq[3].status == "failed"
    assert by_seq[3].outcome == "error"
    assert by_seq[3].reason_code == "DURABLE_CONTROL_WRITE_FAILED"


def test_latest_durable_write_failure_surfaces_until_newer_command(tmp_path: Path) -> None:
    d = tmp_path / "commands"
    d.mkdir()
    _ack(
        d,
        1,
        "PAUSE",
        {
            "status": "error",
            "reason_code": "DURABLE_CONTROL_WRITE_FAILED",
            "effect": "persist desired_state=PAUSED failed",
        },
    )

    assert (
        _resolve_durable_control_write_failure(tmp_path)
        == "persist desired_state=PAUSED failed"
    )

    _ack(d, 2, "FLATTEN", {"status": "success", "effect": "flattened"})
    assert (
        _resolve_durable_control_write_failure(tmp_path)
        == "persist desired_state=PAUSED failed"
    )

    _ack(d, 3, "PAUSE", {"status": "success", "effect": "paused"})
    assert _resolve_durable_control_write_failure(tmp_path) is None


def test_timeline_empty_for_absent_dir(tmp_path: Path) -> None:
    timeline = build_command_timeline(tmp_path / "nope")
    assert timeline.entries == []
    assert timeline.poll_interval_ms == COMMAND_POLL_INTERVAL_MS
