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

from pydantic import BaseModel, ConfigDict

_SEQ_RE = re.compile(r"^command\.(\d+)\.")


class CommandVerb(StrEnum):
    PAUSE = "PAUSE"


class Command(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int
    verb: CommandVerb


class CommandChannel:
    def __init__(self, commands_dir: Path) -> None:
        self._dir = commands_dir

    def write_from_operator(self, verb: CommandVerb) -> Command:
        """Atomically publish a command: write to a sibling .tmp, fsync,
        os.replace to .pending.json.

        Readers glob only `.pending.json` so the in-flight `.tmp` is
        never visible. A crash before the rename leaves no
        `.pending.json` to dispatch; a crash after the rename leaves a
        complete command. There is no partial state the reader can see.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        seq = self._next_seq()
        cmd = Command(seq=seq, verb=verb)
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
        if not self._dir.exists():
            return []
        return [
            Command.model_validate_json(p.read_text(encoding="utf-8"))
            for p in sorted(self._dir.glob("command.*.pending.json"))
        ]

    def ack(self, command: Command) -> None:
        """Atomically transition pending → ack for one command.

        Renames the pending.json filename in place. Future readers'
        read_pending() will no longer return this command; the ack.json
        is the audit-trail record that the engine acted on it.
        """
        pending = self._dir / f"command.{command.seq}.{command.verb.value}.pending.json"
        ack = self._dir / f"command.{command.seq}.{command.verb.value}.ack.json"
        os.replace(pending, ack)
