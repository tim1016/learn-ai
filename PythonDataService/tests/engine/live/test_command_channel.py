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


def test_orphan_pending_tmp_is_invisible_to_read_pending(tmp_path: Path) -> None:
    """A leftover .pending.tmp from a crashed write is ignored.

    Atomic-rename invariant: only `.json` files are visible to readers;
    `.tmp` files are intermediate state.
    """
    commands_dir = tmp_path / "commands"
    commands_dir.mkdir(parents=True)
    # Hand-craft a stray .tmp file as if a previous write crashed mid-flight.
    (commands_dir / "command.1.PAUSE.pending.tmp").write_text(
        "{ partial garbage", encoding="utf-8"
    )
    channel = CommandChannel(commands_dir)
    assert channel.read_pending() == []


def test_write_uses_tempfile_rename_pattern(
    tmp_path: Path, monkeypatch: __import__("pytest").MonkeyPatch
) -> None:
    """If os.replace fails mid-rename, no .pending.json is left behind
    and the directory shows no orphan .tmp."""
    import os as _os

    channel = CommandChannel(tmp_path / "commands")

    def failing_replace(src: str, dst: str) -> None:
        raise OSError("simulated crash before rename")

    monkeypatch.setattr(_os, "replace", failing_replace)
    import pytest as _pytest

    with _pytest.raises(OSError):
        channel.write_from_operator(CommandVerb.PAUSE)
    monkeypatch.undo()

    assert list((tmp_path / "commands").glob("*.pending.json")) == []
    assert list((tmp_path / "commands").glob("*.pending.tmp")) == []
