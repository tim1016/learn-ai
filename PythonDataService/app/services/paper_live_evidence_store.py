"""Durable comparison evidence for #2371, separate from fleet and custody stores.

This is a read-only consumer of Clerk observations, never an order, deployment,
or arming authority. The collector must freeze and verify each source identity
and observe revisions to existing receipts, not merely poll after the last seq.

One transaction commits receipt revisions, source watermarks and the derived
session summaries. Missing sequences remain missing even if both lanes have
identical surviving tails. WAL + FULL synchronization follow the existing
control-volume SQLite discipline; the research ArtifactStore's independent
config/result replacements cannot provide this multi-record transaction.

Formula: missing receipts = source sequence watermark - distinct archived
  sequences (the Clerk allocates consecutively from 1); conflicts count sticky
  identity/trace revisions. Comparison counts delegate to paper_live_comparison.
Reference: #2371; app/broker/alpaca/clerk/sqlite/decision_receipts.py allocates
  sequences and replaces provisional outcomes at the same sequence.
Canonical implementation: this module for archive coverage only.
Validated against: tests/services/test_paper_live_evidence_store.py, exact
  integer counts and identities; no floating-point tolerance applies.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from types import TracebackType
from typing import Self

from app.schemas.paper_live_experiments import (
    ExperimentDecision,
    ExperimentDecisionRevision,
    ExperimentEvidenceCapture,
    ExperimentLane,
    ExperimentLaneCoverage,
    PaperLiveEvidencePair,
    PaperLiveEvidenceReport,
)
from app.services.paper_live_comparison import compare_decisions
from app.utils.advisory_lock import advisory_file_lock

_SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE experiments (
    experiment_id TEXT PRIMARY KEY,
    pair_json TEXT NOT NULL,
    report_json TEXT NOT NULL
);
CREATE TABLE captures (
    experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
    lane TEXT NOT NULL CHECK (lane IN ('paper', 'live')),
    captured_at_ms INTEGER NOT NULL,
    highest_seq INTEGER NOT NULL,
    digest TEXT NOT NULL,
    PRIMARY KEY (experiment_id, lane)
);
CREATE TABLE decisions (
    experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
    lane TEXT NOT NULL CHECK (lane IN ('paper', 'live')),
    seq INTEGER NOT NULL,
    receipt_json TEXT NOT NULL,
    conflicting INTEGER NOT NULL CHECK (conflicting IN (0, 1)),
    PRIMARY KEY (experiment_id, lane, seq)
);
CREATE TABLE revisions (
    experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
    lane TEXT NOT NULL CHECK (lane IN ('paper', 'live')),
    seq INTEGER NOT NULL,
    captured_at_ms INTEGER NOT NULL,
    receipt_json TEXT NOT NULL,
    PRIMARY KEY (experiment_id, lane, seq, captured_at_ms)
);
"""


def _identity_changed(before: ExperimentDecision, after: ExperimentDecision) -> bool:
    # Final outcomes/order refs/reasons may change legitimately. A previously
    # missing identity may be enriched; an established identity cannot change.
    return any(
        getattr(before, key) is not None and getattr(before, key) != getattr(after, key)
        for key in ("run_id", "recorded_at_ms", "decision_bar_close_ms", "trace_digest", "decision_id")
    )


class PaperLiveEvidenceStore:
    """A coordinator-owned archive with transactionally materialized reports.

    All methods are synchronous; an eventual async collector must dispatch them
    off the event loop. No network I/O occurs while a database lock is held.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = RLock()

    @classmethod
    def open(cls, *, control_dir: Path) -> Self:
        database = control_dir / "paper-live-experiments" / "evidence.db"
        database.parent.mkdir(parents=True, exist_ok=True)
        with advisory_file_lock(database):
            conn = sqlite3.connect(database, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA foreign_keys = ON")
                if conn.execute("PRAGMA journal_mode = WAL").fetchone()[0].lower() != "wal":
                    raise ValueError("Experiment evidence requires a local volume supporting SQLite WAL")
                conn.execute("PRAGMA synchronous = FULL")
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table'").fetchone():
                        raise ValueError("Unversioned experiment evidence database is not empty")
                    conn.executescript(
                        f"BEGIN IMMEDIATE;\n{_SCHEMA}\nPRAGMA user_version = {_SCHEMA_VERSION};\nCOMMIT;"
                    )
                elif version != _SCHEMA_VERSION:
                    raise ValueError(f"Unsupported experiment evidence schema version: {version}")
                if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("Experiment evidence database failed its integrity check")
            except BaseException:
                conn.close()
                raise
        return cls(conn)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def create(self, pair: PaperLiveEvidencePair) -> PaperLiveEvidenceReport:
        """Freeze the pair before collecting, retrying only identical identities."""
        with self._transaction():
            existing = self._conn.execute(
                "SELECT pair_json, report_json FROM experiments WHERE experiment_id = ?",
                (pair.experiment_id,),
            ).fetchone()
            if existing is not None:
                if PaperLiveEvidencePair.model_validate_json(existing["pair_json"]) != pair:
                    raise ValueError("The experiment ID already identifies a different evidence pair")
                return PaperLiveEvidenceReport.model_validate_json(existing["report_json"])
            report = self._build_report(pair)
            self._conn.execute(
                "INSERT INTO experiments VALUES (?, ?, ?)",
                (
                    pair.experiment_id,
                    pair.model_dump_json(),
                    report.model_dump_json(),
                ),
            )
            return report

    def read(self, experiment_id: str) -> PaperLiveEvidenceReport:
        with self._lock:
            row = self._conn.execute(
                "SELECT report_json FROM experiments WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown Paper/Live evidence pair: {experiment_id}")
            return PaperLiveEvidenceReport.model_validate_json(row["report_json"])

    def capture(
        self,
        experiment_id: str,
        lane: ExperimentLane,
        observation: ExperimentEvidenceCapture,
    ) -> PaperLiveEvidenceReport:
        """Archive a source snapshot and its new summary in one transaction."""
        with self._transaction():
            report = self.read(experiment_id)
            source = report.pair.paper if lane == "paper" else report.pair.live
            if observation.source != source:
                raise ValueError("The capture source identity does not match the frozen experiment lane")
            if observation.captured_at_ms < report.pair.created_at_ms:
                raise ValueError("The capture predates the experiment")
            digest = hashlib.sha256(observation.model_dump_json().encode()).hexdigest()
            prior = self._conn.execute(
                "SELECT * FROM captures WHERE experiment_id = ? AND lane = ?",
                (experiment_id, lane),
            ).fetchone()
            if prior is not None:
                if observation.captured_at_ms < prior["captured_at_ms"]:
                    raise ValueError("An older capture cannot replace newer experiment evidence")
                if observation.captured_at_ms == prior["captured_at_ms"]:
                    if digest != prior["digest"]:
                        raise ValueError("Different evidence was supplied for the same capture time")
                    return report
                if observation.highest_seq < prior["highest_seq"]:
                    raise ValueError("The source receipt watermark moved backwards")
            for decision in observation.decisions:
                self._archive_decision(experiment_id, lane, observation.captured_at_ms, decision)
            self._conn.execute(
                "INSERT INTO captures VALUES (?, ?, ?, ?, ?) ON CONFLICT (experiment_id, lane) DO UPDATE SET "
                "captured_at_ms = excluded.captured_at_ms, highest_seq = excluded.highest_seq, digest = excluded.digest",
                (experiment_id, lane, observation.captured_at_ms, observation.highest_seq, digest),
            )
            updated = self._build_report(report.pair)
            self._conn.execute(
                "UPDATE experiments SET report_json = ? WHERE experiment_id = ?",
                (
                    updated.model_dump_json(),
                    experiment_id,
                ),
            )
            return updated

    def _archive_decision(
        self,
        experiment_id: str,
        lane: ExperimentLane,
        captured_at_ms: int,
        decision: ExperimentDecision,
    ) -> None:
        key = (experiment_id, lane, decision.seq)
        before = self._conn.execute(
            "SELECT receipt_json, conflicting FROM decisions WHERE experiment_id = ? AND lane = ? AND seq = ?",
            key,
        ).fetchone()
        payload = decision.model_dump_json()
        if before is not None and before["receipt_json"] == payload:
            return
        conflicting = before is not None and (
            bool(before["conflicting"])
            or _identity_changed(
                ExperimentDecision.model_validate_json(before["receipt_json"]),
                decision,
            )
        )
        self._conn.execute("INSERT INTO revisions VALUES (?, ?, ?, ?, ?)", (*key, captured_at_ms, payload))
        self._conn.execute(
            "INSERT INTO decisions VALUES (?, ?, ?, ?, ?) ON CONFLICT (experiment_id, lane, seq) DO UPDATE SET "
            "receipt_json = excluded.receipt_json, conflicting = excluded.conflicting",
            (*key, payload, int(conflicting)),
        )

    def _build_report(self, pair: PaperLiveEvidencePair) -> PaperLiveEvidenceReport:
        receipts: dict[ExperimentLane, list[ExperimentDecision]] = {"paper": [], "live": []}
        conflicts: set[tuple[ExperimentLane, int]] = set()
        for row in self._conn.execute(
            "SELECT * FROM decisions WHERE experiment_id = ? ORDER BY lane, seq",
            (pair.experiment_id,),
        ):
            receipts[row["lane"]].append(ExperimentDecision.model_validate_json(row["receipt_json"]))
            if row["conflicting"]:
                conflicts.add((row["lane"], row["seq"]))
        coverage: dict[ExperimentLane, ExperimentLaneCoverage] = {}
        for lane in receipts:
            state = self._conn.execute(
                "SELECT * FROM captures WHERE experiment_id = ? AND lane = ?",
                (pair.experiment_id, lane),
            ).fetchone()
            highest_seq = state["highest_seq"] if state is not None else 0
            coverage[lane] = ExperimentLaneCoverage(
                captured_at_ms=state["captured_at_ms"] if state is not None else None,
                highest_seq=highest_seq,
                missing_receipts=highest_seq - len(receipts[lane]),
                conflicting_receipts=sum(source == lane for source, _ in conflicts),
            )
        complete = all(
            item.captured_at_ms is not None and item.missing_receipts == 0 and item.conflicting_receipts == 0
            for item in coverage.values()
        )
        return PaperLiveEvidenceReport(
            pair=pair,
            paper=coverage["paper"],
            live=coverage["live"],
            comparison=compare_decisions(
                receipts["paper"],
                receipts["live"],
                evidence_complete=complete,
                conflicting_receipts=frozenset(conflicts),
            ),
        )

    def revisions(
        self,
        experiment_id: str,
        lane: ExperimentLane,
        seq: int,
    ) -> tuple[ExperimentDecisionRevision, ...]:
        with self._lock:
            return tuple(
                ExperimentDecisionRevision(
                    captured_at_ms=row["captured_at_ms"],
                    decision=ExperimentDecision.model_validate_json(row["receipt_json"]),
                )
                for row in self._conn.execute(
                    "SELECT captured_at_ms, receipt_json FROM revisions "
                    "WHERE experiment_id = ? AND lane = ? AND seq = ? ORDER BY captured_at_ms",
                    (experiment_id, lane, seq),
                )
            )
