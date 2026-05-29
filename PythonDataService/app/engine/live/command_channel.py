"""Operator command channel — file-based pending/ack with atomic rename.

Grown vertically via TDD: each cycle adds one verb, one field, or one
mechanic. See plan §16.4 Resolution 7 / "Command channel mechanics" in
the PRD for the full target contract. Engine-side polling (1s loop,
verb dispatch) is consumed by a separate module and out of scope here.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict


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
        seq = 1
        cmd = Command(seq=seq, verb=verb)
        path = self._dir / f"command.{seq}.{verb.value}.pending.json"
        path.write_text(cmd.model_dump_json(), encoding="utf-8")
        return cmd

    def read_pending(self) -> list[Command]:
        if not self._dir.exists():
            return []
        return [
            Command.model_validate_json(p.read_text(encoding="utf-8"))
            for p in sorted(self._dir.glob("command.*.pending.json"))
        ]
