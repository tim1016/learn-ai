"""Durable store for custom Dark Bright Gates, per strategy (#2639 D10).

One JSON file on the data plane holds every strategy's gates, written under an
exclusive lock and replaced atomically. The data plane is the one place the
bot page and Strategy Lab both reach, so a gate saved on a strategy is there
for every bot running it and for its backtests. No clerk reads this file:
custom gates are judged by the data plane (``app.services.strategy_gates``).
"""

from __future__ import annotations

import fcntl
import json
import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.strategy_gates import CustomGate
from app.utils.atomic_file import atomic_write_bytes

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GATE_STORE_PATH = _SERVICE_ROOT / "artifacts" / "strategy_view" / "custom_gates.json"


class GateStoreError(RuntimeError):
    """The gate file could not be read or written."""


class GateNotFoundError(LookupError):
    """No gate with this id is saved on this strategy."""


class _GateFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    gates: list[CustomGate] = Field(default_factory=list)


class StrategyGateStore:
    """Create, list, replace and delete the gates saved on a strategy."""

    def __init__(self, path: Path = DEFAULT_GATE_STORE_PATH) -> None:
        self._path = path

    def for_strategy(self, strategy_key: str) -> list[CustomGate]:
        return [gate for gate in self._read().gates if gate.strategy_key == strategy_key]

    def get(self, strategy_key: str, gate_id: str) -> CustomGate:
        for gate in self.for_strategy(strategy_key):
            if gate.gate_id == gate_id:
                return gate
        raise GateNotFoundError(f"No gate '{gate_id}' is saved on '{strategy_key}'.")

    def create(self, build: Callable[[str], CustomGate]) -> CustomGate:
        """Save the gate ``build`` makes from a fresh gate id."""
        with self._locked() as current:
            taken = {gate.gate_id for gate in current.gates}
            gate_id = _new_gate_id(taken)
            gate = build(gate_id)
            current.gates.append(gate)
            self._write(current)
        return gate

    def replace(self, gate: CustomGate) -> CustomGate:
        with self._locked() as current:
            index = _index_of(current, gate.strategy_key, gate.gate_id)
            current.gates[index] = gate
            self._write(current)
        return gate

    def delete(self, strategy_key: str, gate_id: str) -> None:
        with self._locked() as current:
            del current.gates[_index_of(current, strategy_key, gate_id)]
            self._write(current)

    @contextmanager
    def _locked(self) -> Iterator[_GateFile]:
        lock_path = self._path.with_suffix(f"{self._path.suffix}.lock")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            lock_file = lock_path.open("a+", encoding="utf-8")
        except OSError as exc:
            raise GateStoreError(f"The saved gates at {self._path.name} could not be locked: {exc}") from exc
        with lock_file:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            except OSError as exc:
                raise GateStoreError(f"The saved gates at {self._path.name} could not be locked: {exc}") from exc
            try:
                yield self._read()
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read(self) -> _GateFile:
        if not self._path.exists():
            return _GateFile()
        try:
            raw: Any = json.loads(self._path.read_text(encoding="utf-8"))
            return _GateFile.model_validate(raw)
        except (OSError, ValueError) as exc:
            raise GateStoreError(f"The saved gates at {self._path.name} could not be read: {exc}") from exc

    def _write(self, current: _GateFile) -> None:
        try:
            atomic_write_bytes(self._path, (current.model_dump_json(indent=2) + "\n").encode("utf-8"))
        except OSError as exc:
            raise GateStoreError(f"The saved gates at {self._path.name} could not be written: {exc}") from exc


def _index_of(current: _GateFile, strategy_key: str, gate_id: str) -> int:
    for index, gate in enumerate(current.gates):
        if gate.strategy_key == strategy_key and gate.gate_id == gate_id:
            return index
    raise GateNotFoundError(f"No gate '{gate_id}' is saved on '{strategy_key}'.")


def _new_gate_id(taken: set[str]) -> str:
    while True:
        candidate = f"g-{secrets.token_hex(6)}"
        if candidate not in taken:
            return candidate


__all__ = ["DEFAULT_GATE_STORE_PATH", "GateNotFoundError", "GateStoreError", "StrategyGateStore"]
