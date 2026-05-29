"""Tests for CommandChannel — file-based atomic pending/ack mechanics.

Engine-side wiring (1s poll loop, STOP→shutdown, MARK_POISONED→poisoned.flag)
is consumed by a separate module and out of scope here. This file only
exercises the channel's read/ack/write contract.
"""

from __future__ import annotations

from pathlib import Path

from app.engine.live.command_channel import CommandChannel, CommandVerb


def test_write_then_read_pending_returns_single_pause(tmp_path: Path) -> None:
    channel = CommandChannel(tmp_path / "commands")
    channel.write_from_operator(CommandVerb.PAUSE)
    pending = channel.read_pending()
    assert len(pending) == 1
    assert pending[0].verb is CommandVerb.PAUSE


def test_consecutive_writes_assign_monotonic_seq(tmp_path: Path) -> None:
    channel = CommandChannel(tmp_path / "commands")
    first = channel.write_from_operator(CommandVerb.PAUSE)
    second = channel.write_from_operator(CommandVerb.PAUSE)
    assert first.seq == 1
    assert second.seq == 2

    pending = channel.read_pending()
    assert [p.seq for p in pending] == [1, 2]
