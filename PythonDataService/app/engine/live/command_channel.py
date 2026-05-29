"""Operator command channel — file-based pending/ack with atomic rename.

Grown vertically via TDD: each cycle adds one verb, one field, or one
mechanic. See plan §16.4 Resolution 7 / "Command channel mechanics" in
the PRD for the full target contract. Engine-side polling (1s loop,
verb dispatch) is consumed by a separate module and out of scope here.
"""

from __future__ import annotations

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
        self._dir.mkdir(parents=True, exist_ok=True)
        seq = self._next_seq()
        cmd = Command(seq=seq, verb=verb)
        path = self._dir / f"command.{seq}.{verb.value}.pending.json"
        path.write_text(cmd.model_dump_json(), encoding="utf-8")
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
