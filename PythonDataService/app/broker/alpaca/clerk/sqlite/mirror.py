"""Write-only mirror — the two-phase fsync fence pinned in contracts doc §8.

Never read on the hot path (R9). Consulted only at startup (finalize-gap
check, §9 check 9) and during disaster recovery (``rebuild``, §9.3/§13).

One mirror file is scoped to exactly one authority generation (rotation is
the repository's job at reset time, not this module's).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.broker.alpaca.clerk.sqlite.hashchain import GENESIS, compute_row_hash


class MirrorChainBroken(Exception):
    """A sequence gap, duplicate-with-different-hash, or hash mismatch.

    Per §8: "fails closed rather than importing ambiguous data." Callers
    must not catch this and proceed — it means the account stays failed
    closed until a human recovery workflow runs.
    """


@dataclass(frozen=True)
class PendingTransition:
    """Everything ``mirror.prepare`` needs before the owning SQLite commit."""

    sequence: int
    authority_generation: int
    row_hash: str
    prev_hash: str
    payload_canonical: str
    recorded_at_ms: int


@dataclass(frozen=True)
class RebuiltRow:
    """One transition reconstructed purely from finalized mirror records."""

    sequence: int
    authority_generation: int
    row_hash: str
    prev_hash: str
    payload: dict[str, Any]
    recorded_at_ms: int


class MirrorFile:
    """The append-only ``custody_transitions.mirror`` file for one account."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def prepare(self, pending: PendingTransition) -> None:
        """Fsync a PREPARE line. Must complete before the SQLite transaction opens."""
        line = {
            "phase": "PREPARE",
            "sequence": pending.sequence,
            "authority_generation": pending.authority_generation,
            "row_hash": pending.row_hash,
            "prev_hash": pending.prev_hash,
            "payload_canonical": pending.payload_canonical,
            "recorded_at_ms": pending.recorded_at_ms,
        }
        self._append_fsynced(line)

    def finalize(
        self,
        *,
        sequence: int,
        authority_generation: int,
        row_hash: str,
        recorded_at_ms: int,
    ) -> None:
        """Fsync the matching FINALIZE line. Must run after the SQLite commit."""
        line = {
            "phase": "FINALIZE",
            "sequence": sequence,
            "authority_generation": authority_generation,
            "row_hash": row_hash,
            "recorded_at_ms": recorded_at_ms,
        }
        self._append_fsynced(line)

    def _append_fsynced(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def _read_records(self) -> list[dict[str, Any]]:
        if not self._path.is_file():
            return []
        with self._path.open(encoding="utf-8") as handle:
            return [json.loads(stripped) for raw in handle if (stripped := raw.strip())]

    def has_finalize(self, sequence: int) -> bool:
        """Startup check 9: does ``sequence`` have a matching FINALIZE line?"""
        return any(
            r["phase"] == "FINALIZE" and r["sequence"] == sequence
            for r in self._read_records()
        )

    def rebuild(self) -> list[RebuiltRow]:
        """Replay finalized PREPARE/FINALIZE pairs into rows, verifying the chain.

        Fails closed (raises ``MirrorChainBroken``) on: a PREPARE with no
        matching FINALIZE (excluded, not an error by itself — it's just
        dropped, since the fence guarantees it had no broker effect), a
        sequence gap, a duplicate sequence with a different hash, or a
        hash-chain break.
        """
        records = self._read_records()
        prepares: dict[int, dict[str, Any]] = {}
        finalized: set[int] = set()
        for record in records:
            sequence = record["sequence"]
            if record["phase"] == "PREPARE":
                if sequence in prepares and prepares[sequence]["row_hash"] != record["row_hash"]:
                    raise MirrorChainBroken(
                        f"duplicate sequence {sequence} with a different hash"
                    )
                prepares[sequence] = record
            elif record["phase"] == "FINALIZE":
                if sequence in prepares and prepares[sequence]["row_hash"] != record["row_hash"]:
                    raise MirrorChainBroken(
                        f"sequence {sequence} FINALIZE hash disagrees with its PREPARE"
                    )
                finalized.add(sequence)
            else:  # pragma: no cover - defensive, format is pinned
                raise MirrorChainBroken(f"unknown mirror record phase: {record['phase']!r}")

        usable_sequences = sorted(s for s in finalized if s in prepares)
        expected_prev = GENESIS
        rows: list[RebuiltRow] = []
        for expected_seq, sequence in enumerate(usable_sequences, start=1):
            if sequence != expected_seq:
                raise MirrorChainBroken(
                    f"sequence gap: expected {expected_seq}, found {sequence}"
                )
            record = prepares[sequence]
            recomputed = compute_row_hash(expected_prev, record["payload_canonical"])
            if record["prev_hash"] != expected_prev or record["row_hash"] != recomputed:
                raise MirrorChainBroken(f"hash-chain break at sequence {sequence}")
            rows.append(
                RebuiltRow(
                    sequence=sequence,
                    authority_generation=record["authority_generation"],
                    row_hash=record["row_hash"],
                    prev_hash=record["prev_hash"],
                    payload=json.loads(record["payload_canonical"]),
                    recorded_at_ms=record["recorded_at_ms"],
                )
            )
            expected_prev = record["row_hash"]
        return rows
