"""Tests for CommandChannel — file-based atomic pending/ack mechanics.

Engine-side wiring (1s poll loop, STOP→shutdown, MARK_POISONED→poisoned.flag)
is consumed by a separate module and out of scope here. This file only
exercises the channel's read/ack/write contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.engine.live.command_channel import (
    CommandChannel,
    CommandChannelCorruptError,
    CommandVerb,
)


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


@pytest.mark.parametrize(
    "verb",
    [
        CommandVerb.PAUSE,
        CommandVerb.RESUME,
        CommandVerb.STOP,
        CommandVerb.FLATTEN,
        CommandVerb.RECONCILE,
        CommandVerb.MARK_POISONED,
    ],
)
def test_full_verb_vocabulary_round_trips(tmp_path: Path, verb: CommandVerb) -> None:
    channel = CommandChannel(tmp_path / "commands")
    channel.write_from_operator(verb)
    [pending] = channel.read_pending()
    assert pending.verb is verb


def test_command_carries_payload_round_trip(tmp_path: Path) -> None:
    channel = CommandChannel(tmp_path / "commands")
    channel.write_from_operator(
        CommandVerb.MARK_POISONED,
        payload={"reason": "manual_trade_observed", "noticed_at_ms": 1_748_000_000_000},
    )
    [pending] = channel.read_pending()
    assert pending.payload == {
        "reason": "manual_trade_observed",
        "noticed_at_ms": 1_748_000_000_000,
    }


def test_command_payload_defaults_to_empty_dict(tmp_path: Path) -> None:
    channel = CommandChannel(tmp_path / "commands")
    channel.write_from_operator(CommandVerb.PAUSE)
    [pending] = channel.read_pending()
    assert pending.payload == {}


def test_ack_renames_pending_to_ack_and_clears_pending_queue(tmp_path: Path) -> None:
    channel = CommandChannel(tmp_path / "commands")
    channel.write_from_operator(CommandVerb.PAUSE)
    [pending_cmd] = channel.read_pending()

    channel.ack(pending_cmd)

    assert channel.read_pending() == []
    assert list((tmp_path / "commands").glob("*.pending.json")) == []
    ack_files = list((tmp_path / "commands").glob("*.ack.json"))
    assert len(ack_files) == 1
    assert ack_files[0].name == f"command.{pending_cmd.seq}.{pending_cmd.verb.value}.ack.json"


def test_ack_outcome_payload_persists_in_ack_file(tmp_path: Path) -> None:
    channel = CommandChannel(tmp_path / "commands")
    channel.write_from_operator(
        CommandVerb.MARK_POISONED, payload={"reason": "manual_trade_observed"}
    )
    [pending] = channel.read_pending()

    channel.ack(
        pending,
        outcome={"status": "success", "side_effect": "wrote poisoned.flag"},
    )

    [ack_path] = list((tmp_path / "commands").glob("*.ack.json"))
    data = json.loads(ack_path.read_text(encoding="utf-8"))
    assert data["seq"] == pending.seq
    assert data["verb"] == "MARK_POISONED"
    assert data["payload"] == {"reason": "manual_trade_observed"}
    assert data["outcome"] == {"status": "success", "side_effect": "wrote poisoned.flag"}


def test_unparseable_pending_json_raises_typed_error(tmp_path: Path) -> None:
    commands_dir = tmp_path / "commands"
    commands_dir.mkdir(parents=True)
    bad = commands_dir / "command.1.PAUSE.pending.json"
    bad.write_text("{ not json", encoding="utf-8")
    channel = CommandChannel(commands_dir)
    with pytest.raises(CommandChannelCorruptError) as excinfo:
        channel.read_pending()
    assert excinfo.value.path == bad


def test_schema_violation_in_pending_raises_typed_error(tmp_path: Path) -> None:
    commands_dir = tmp_path / "commands"
    commands_dir.mkdir(parents=True)
    bad = commands_dir / "command.1.PAUSE.pending.json"
    bad.write_text(
        '{"seq": 1, "verb": "NOT_A_REAL_VERB", "payload": {}}', encoding="utf-8"
    )
    channel = CommandChannel(commands_dir)
    with pytest.raises(CommandChannelCorruptError):
        channel.read_pending()


def test_read_pending_sorts_by_numeric_seq_not_filename(tmp_path: Path) -> None:
    """Seq 10 sorts after seq 9, not after seq 1, regardless of filename
    lexicographic order."""
    channel = CommandChannel(tmp_path / "commands")
    for _ in range(12):
        channel.write_from_operator(CommandVerb.PAUSE)
    pending = channel.read_pending()
    assert [p.seq for p in pending] == list(range(1, 13))


def test_ack_outcome_defaults_to_empty_dict(tmp_path: Path) -> None:
    channel = CommandChannel(tmp_path / "commands")
    channel.write_from_operator(CommandVerb.PAUSE)
    [pending] = channel.read_pending()
    channel.ack(pending)
    [ack_path] = list((tmp_path / "commands").glob("*.ack.json"))
    data = json.loads(ack_path.read_text(encoding="utf-8"))
    assert data["outcome"] == {}


def test_concurrent_writers_produce_unique_seqs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two threads each writing N commands must produce 2N distinct
    seqs. Without a lock around (next_seq + rename) they can both read
    max_seen and assign the same seq, overwriting each other.

    The test injects a yield (time.sleep(0)) inside _next_seq to widen
    the race window past the GIL's accidental serialisation. The lock
    must close that window.
    """
    import threading
    import time

    from app.engine.live import command_channel as cc_module

    real_next_seq = cc_module.CommandChannel._next_seq

    def instrumented_next_seq(self: CommandChannel) -> int:
        seq = real_next_seq(self)
        time.sleep(0.005)  # widen race window past the GIL's accidental serialisation
        return seq

    monkeypatch.setattr(cc_module.CommandChannel, "_next_seq", instrumented_next_seq)

    channel = CommandChannel(tmp_path / "commands")
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            for _ in range(20):
                channel.write_from_operator(CommandVerb.PAUSE)
        except BaseException as exc:
            errors.append(exc)

    t1 = threading.Thread(target=writer)
    t2 = threading.Thread(target=writer)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert errors == [], f"concurrent writers raised: {errors!r}"
    pending = channel.read_pending()
    seqs = [p.seq for p in pending]
    assert sorted(seqs) == list(range(1, 41)), f"got seqs={sorted(seqs)}"


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
