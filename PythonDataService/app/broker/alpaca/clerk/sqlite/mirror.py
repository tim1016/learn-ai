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
from app.broker.alpaca.paths import fsync_directory
from app.utils.timestamps import Clock


class MirrorChainBroken(Exception):
    """A sequence gap, duplicate-with-different-hash, or hash mismatch.

    Per §8: "fails closed rather than importing ambiguous data." Callers
    must not catch this and proceed — it means the account stays failed
    closed until a human recovery workflow runs.
    """


@dataclass(frozen=True)
class MirrorIdentity:
    """Generation binding that makes an otherwise valid mirror non-portable."""

    account_id: str
    authority_generation: int
    db_identity_token: str


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

    def __init__(self, path: Path, *, expected_identity: MirrorIdentity | None = None) -> None:
        self._path = path
        self._expected_identity = expected_identity

    @property
    def path(self) -> Path:
        return self._path

    def initialize_identity(self) -> None:
        """Write the immutable account/generation binding before any PREPARE."""
        if self._expected_identity is None:
            raise ValueError("expected_identity is required to initialize a mirror binding")
        if self._path.exists():
            raise MirrorChainBroken(f"cannot initialize identity for existing mirror {self._path}")
        self._append_fsynced(
            {
                "phase": "IDENTITY",
                "account_id": self._expected_identity.account_id,
                "authority_generation": self._expected_identity.authority_generation,
                "db_identity_token": self._expected_identity.db_identity_token,
            }
        )

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
        existed = self._path.is_file()
        line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        if not existed:
            # The directory-creation fsync (initialize()) predates this
            # file's existence and does not cover its own directory entry —
            # a separate durability gap the corrective foundation slice
            # closes (open-pr-review-2026-08-05.md P2 "First mirror creation
            # lacks parent-directory fsync").
            fsync_directory(self._path.parent)

    def _read_records(self) -> list[dict[str, Any]]:
        if not self._path.is_file():
            return []
        with self._path.open(encoding="utf-8") as handle:
            return [json.loads(stripped) for raw in handle if (stripped := raw.strip())]

    def has_finalize(self, sequence: int) -> bool:
        """Does ``sequence`` have a matching FINALIZE line?"""
        return any(
            r["phase"] == "FINALIZE" and r["sequence"] == sequence
            for r in self._read_records()
        )

    def _parsed_records(
        self,
    ) -> tuple[dict[int, list[dict[str, Any]]], dict[int, dict[str, Any]]]:
        """Parse every mirror line into ``(prepares_by_sequence,
        finalize_by_sequence)`` — the one shared parse both ``reconcile()``
        (startup) and ``rebuild()`` (disaster recovery) verify against, so
        the two integrity policies cannot silently diverge (open-pr-review-2026-08-05.md
        P2 "use the same verifier for startup and rebuild"). Two FINALIZE
        records at the same sequence with different hashes is genuine
        corruption and fails closed here, before either caller decides what
        to do with the parsed result.
        """
        records = self._read_records()
        identity_records = [record for record in records if record.get("phase") == "IDENTITY"]
        if self._expected_identity is not None:
            if len(identity_records) != 1:
                raise MirrorChainBroken("mirror must contain exactly one identity record")
            identity = identity_records[0]
            observed = MirrorIdentity(
                account_id=identity.get("account_id"),
                authority_generation=identity.get("authority_generation"),
                db_identity_token=identity.get("db_identity_token"),
            )
            if observed != self._expected_identity:
                raise MirrorChainBroken(
                    "mirror account, generation, or database identity does not match registry"
                )
        elif len(identity_records) > 1:
            raise MirrorChainBroken("mirror contains duplicate identity records")

        prepares_by_sequence: dict[int, list[dict[str, Any]]] = {}
        finalize_by_sequence: dict[int, dict[str, Any]] = {}
        for record in records:
            if record.get("phase") == "IDENTITY":
                continue
            if "sequence" not in record:
                raise MirrorChainBroken("mirror transition record has no sequence")
            sequence = record["sequence"]
            if record["phase"] == "PREPARE":
                prepares_by_sequence.setdefault(sequence, []).append(record)
            elif record["phase"] == "FINALIZE":
                existing = finalize_by_sequence.get(sequence)
                if existing is not None and existing["row_hash"] != record["row_hash"]:
                    raise MirrorChainBroken(
                        f"sequence {sequence} has conflicting FINALIZE records"
                    )
                finalize_by_sequence[sequence] = record
            else:  # pragma: no cover - defensive, format is pinned
                raise MirrorChainBroken(f"unknown mirror record phase: {record['phase']!r}")
        return prepares_by_sequence, finalize_by_sequence

    def reconcile(self, committed_rows: list[dict[str, Any]], *, clock: Clock) -> None:
        """Startup check 9, corrected: every committed row, not only the
        tail (open-pr-review-2026-08-05.md P2 "Only the mirror tail is
        checked" — a tail-only check leaves an earlier unrecoverable gap
        invisible).

        A row with no matching FINALIZE is finalized now from the row's own
        committed data — a crash between §4 steps 2 and 3 leaves the DB
        transaction as the durable fact and the mirror only catching up, so
        completing the fence here (rather than failing closed) is correct
        for every such row, not only the most recent one — **but only when
        the mirror still has the matching PREPARE** (with this row's exact
        hash): completing a FINALIZE from the DB row alone, with no PREPARE
        to back it, would pass startup while leaving the mirror unable to
        ever reconstruct that row's ``payload_canonical`` again, silently
        destroying the disaster-recovery property R9 exists to guarantee
        (open-pr-review-2026-08-05.md P2 "require PREPARE before catching up
        FINALIZE"). A row whose existing FINALIZE disagrees with the
        committed row (different hash or generation) is genuine corruption
        and fails closed.
        """
        prepares_by_sequence, finalize_by_sequence = self._parsed_records()
        for row in committed_rows:
            existing = finalize_by_sequence.get(row["sequence"])
            if existing is None:
                matching_prepares = [
                    record
                    for record in prepares_by_sequence.get(row["sequence"], [])
                    if record["row_hash"] == row["row_hash"]
                ]
                if not matching_prepares:
                    raise MirrorChainBroken(
                        f"sequence {row['sequence']} is committed but has no matching "
                        "PREPARE to finalize from — mirror cannot recover this row"
                    )
                self.finalize(
                    sequence=row["sequence"],
                    authority_generation=row["authority_generation"],
                    row_hash=row["row_hash"],
                    recorded_at_ms=clock(),
                )
            elif (
                existing["row_hash"] != row["row_hash"]
                or existing["authority_generation"] != row["authority_generation"]
            ):
                raise MirrorChainBroken(
                    f"sequence {row['sequence']} has a FINALIZE record disagreeing "
                    "with the committed row"
                )

    def rebuild(self) -> list[RebuiltRow]:
        """Replay finalized PREPARE/FINALIZE pairs into rows, verifying the chain.

        A sequence number can legitimately have more than one PREPARE line:
        whenever a prepared transition's owning SQLite commit fails (a
        fold-level constraint violation on a concurrent decision, for
        instance — see #1376's review), the mirror already has an fsynced
        PREPARE for that sequence, but ``append_transition`` computes its
        next sequence from the *committed* row count, so the very next
        successful append reuses the same number with a different payload.
        That abandoned PREPARE never gets a FINALIZE — the fence's own
        guarantee that it had no broker effect (R1) — and must not be
        mistaken for corruption; only the PREPARE whose hash matches the
        sequence's FINALIZE is real, so a FINALIZE (not a raw duplicate
        PREPARE) is what makes a sequence "used" here.

        Fails closed (raises ``MirrorChainBroken``) on: two FINALIZE
        records at the same sequence with different hashes, a FINALIZE
        with no matching-hash PREPARE, a sequence gap in the finalized
        chain, or a hash-chain break.
        """
        prepares_by_sequence, finalize_by_sequence = self._parsed_records()
        usable_sequences = sorted(finalize_by_sequence)
        expected_prev = GENESIS
        rows: list[RebuiltRow] = []
        for expected_seq, sequence in enumerate(usable_sequences, start=1):
            if sequence != expected_seq:
                raise MirrorChainBroken(
                    f"sequence gap: expected {expected_seq}, found {sequence}"
                )
            finalize_record = finalize_by_sequence[sequence]
            matching_prepares = [
                record
                for record in prepares_by_sequence.get(sequence, [])
                if record["row_hash"] == finalize_record["row_hash"]
            ]
            if not matching_prepares:
                raise MirrorChainBroken(f"sequence {sequence} FINALIZE has no matching PREPARE")
            record = matching_prepares[0]
            # Belt-and-suspenders beyond row_hash equality (open-pr-review-2026-08-05.md
            # P2 "require authority-generation equality in the pair"): the two
            # records' own top-level authority_generation fields must agree,
            # not only their (generation-derived) row_hash.
            if record["authority_generation"] != finalize_record["authority_generation"]:
                raise MirrorChainBroken(
                    f"sequence {sequence} PREPARE/FINALIZE authority_generation mismatch"
                )
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
