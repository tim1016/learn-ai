"""Operator command channel — file-based pending/ack with atomic rename.

Grown vertically via TDD: each cycle adds one verb, one field, or one
mechanic. See plan §16.4 Resolution 7 / "Command channel mechanics" in
the PRD for the full target contract. Engine-side polling (1s loop,
verb dispatch) is consumed by a separate module and out of scope here.
"""

from __future__ import annotations

import contextlib
import os
import re
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class CommandChannelCorruptError(RuntimeError):
    """Raised by ``CommandChannel.read_pending`` when an on-disk
    command file is unparseable JSON or fails schema validation.

    Routes to engine-side dispatch as a hard stop: a corrupt command
    cannot be safely executed and demands operator inspection.
    """

    def __init__(self, path: Path, cause: BaseException) -> None:
        super().__init__(f"command at {path} is unreadable: {cause}")
        self.path = path
        self.__cause__ = cause

_SEQ_RE = re.compile(r"^command\.(\d+)\.")


class CommandVerb(StrEnum):
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    STOP = "STOP"
    FLATTEN = "FLATTEN"
    RECONCILE = "RECONCILE"
    MARK_POISONED = "MARK_POISONED"


class Command(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int
    verb: CommandVerb
    payload: dict[str, Any] = Field(default_factory=dict)


class AckedCommand(BaseModel):
    """On-disk shape for an acknowledged command.

    Schema = pending Command + outcome payload. The outcome records what
    the engine did when it dispatched the command: success / error
    message / downstream side effects ("wrote poisoned.flag", etc.).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int
    verb: CommandVerb
    payload: dict[str, Any] = Field(default_factory=dict)
    outcome: dict[str, Any] = Field(default_factory=dict)


class CommandChannel:
    def __init__(self, commands_dir: Path) -> None:
        self._dir = commands_dir

    def write_from_operator(
        self, verb: CommandVerb, *, payload: dict[str, Any] | None = None
    ) -> Command:
        """Atomically publish a command: write to a sibling .tmp, fsync,
        os.replace to .pending.json.

        Readers glob only `.pending.json` so the in-flight `.tmp` is
        never visible. A crash before the rename leaves no
        `.pending.json` to dispatch; a crash after the rename leaves a
        complete command. There is no partial state the reader can see.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        seq = self._next_seq()
        cmd = Command(seq=seq, verb=verb, payload=payload or {})
        final = self._dir / f"command.{seq}.{verb.value}.pending.json"
        tmp = final.with_suffix(".json.tmp")
        payload = cmd.model_dump_json().encode("utf-8")
        with open(tmp, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.replace(tmp, final)
        except Exception:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
        return cmd

    def _next_seq(self) -> int:
        """Highest existing seq + 1 across pending and ack files.

        Scanning ack files too means an op that has already been processed
        doesn't have its seq reused; the audit trail stays unambiguous.
        """
        max_seen = 0
        for entry in self._dir.iterdir():
            match = _SEQ_RE.match(entry.name)
            if match is None:
                continue
            max_seen = max(max_seen, int(match.group(1)))
        return max_seen + 1

    def read_pending(self) -> list[Command]:
        """Return pending commands ordered by numeric seq.

        Lexicographic sort on the filename would put seq 10 before seq
        2; the dispatcher needs them in the order the operator issued
        them, so sort by the seq embedded in the filename.
        """
        if not self._dir.exists():
            return []
        pending_files: list[tuple[int, Path]] = []
        for entry in self._dir.glob("command.*.pending.json"):
            match = _SEQ_RE.match(entry.name)
            if match is None:
                continue
            pending_files.append((int(match.group(1)), entry))
        pending_files.sort(key=lambda item: item[0])
        commands: list[Command] = []
        for _seq, path in pending_files:
            try:
                commands.append(
                    Command.model_validate_json(path.read_text(encoding="utf-8"))
                )
            except (ValidationError, ValueError) as exc:
                raise CommandChannelCorruptError(path, exc) from exc
        return commands

    def ack(self, command: Command, *, outcome: dict[str, Any] | None = None) -> None:
        """Atomically transition pending → ack with the engine's outcome.

        Writes the AckedCommand to ack.tmp, os.replaces it into the
        canonical ack.json, then unlinks the original pending.json. The
        outcome captures what dispatch did (success, error message,
        side effects) so the audit trail is self-describing.
        """
        pending = self._dir / f"command.{command.seq}.{command.verb.value}.pending.json"
        ack_final = self._dir / f"command.{command.seq}.{command.verb.value}.ack.json"
        acked = AckedCommand(
            seq=command.seq,
            verb=command.verb,
            payload=command.payload,
            outcome=outcome or {},
        )
        ack_tmp = ack_final.with_suffix(".json.tmp")
        payload_bytes = acked.model_dump_json().encode("utf-8")
        with open(ack_tmp, "wb") as fh:
            fh.write(payload_bytes)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.replace(ack_tmp, ack_final)
        except Exception:
            with contextlib.suppress(OSError):
                ack_tmp.unlink()
            raise
        with contextlib.suppress(FileNotFoundError):
            pending.unlink()
