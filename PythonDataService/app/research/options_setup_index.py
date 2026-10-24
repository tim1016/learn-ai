"""One read that lists the saved setups and every piece of Options evidence together (#3512, PRD #3490 R20).

The owners stay where they are: saved names in ``options_backtest/saved_setups``, runs in ``saved_runs``, studies in
``options_golden_search/repository`` and approved versions in ``options_bots/golden_repository``. This puts what each
froze (the ``setup_sha256`` of its setup) beside the others, so the page can say which evidence holds the same content
as a saved setup. It joins nothing itself and picks no owner: two names with identical content carry the same hash.

A row whose stored setup can no longer be read stays listed, without a hash and with the reason, as a saved setup that
cannot be opened does. One unreadable record never takes the others with it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from typing import Any

from app.options_backtest import saved_runs, saved_setups
from app.options_backtest.saved_runs import RunRecord
from app.options_backtest.saved_setups import SavedSetup
from app.options_bots.golden import qualification_status
from app.options_bots.golden_proof import GoldenProofError, current_rules_digest, setup_sha256
from app.options_bots.golden_repository import GoldenEvent, GoldenRecord, PostgresGoldenStore
from app.research import options_evidence
from app.research.options_candidate_evidence import retained_protocol
from app.research.options_golden_search import repository as studies
from app.research.persistence import lifecycle
from app.research.persistence.db import with_connection
from app.schemas.options_setup_index import (
    IndexedApproval,
    IndexedRun,
    IndexedSetup,
    IndexedStudy,
    OptionsSetupIndexResponse,
)
from app.utils.timestamps import now_ms_utc

# Each list is cut here, newest first, and says how many exist: a bound that is stated, never a silent first page.
INDEX_LIMIT = 1000


def indexed_setups(saved: Sequence[SavedSetup]) -> list[IndexedSetup]:
    return [
        IndexedSetup(
            name=entry.name,
            example=entry.example,
            saved_at_ms=entry.saved_at_ms,
            underlying=None if entry.setup is None else entry.setup.underlying,
            setup_sha256=None if entry.setup is None else setup_sha256(entry.setup),
            cannot_open_reason=entry.cannot_open_reason,
        )
        for entry in saved
    ]


def indexed_runs(records: Sequence[RunRecord], stored: Mapping[int, SavedSetup]) -> list[IndexedRun]:
    runs: list[IndexedRun] = []
    for record in records:
        frozen = stored.get(record.id)
        runs.append(
            IndexedRun(
                id=record.id,
                recorded_name=record.setup_name,
                underlying=record.underlying,
                setup_sha256=None if frozen is None or frozen.setup is None else setup_sha256(frozen.setup),
                unreadable_reason=None if frozen is None else frozen.cannot_open_reason,
                created_at_ms=record.created_at_ms,
                status=record.status,
                first_session_open_ms=record.first_session_open_ms,
                last_session_open_ms=record.last_session_open_ms,
                sessions_requested=record.sessions_requested,
                net_profit_usd=None if record.headline is None else format(record.headline.net_profit_usd, "f"),
            )
        )
    return runs


def indexed_studies(rows: Sequence[Mapping[str, Any]]) -> list[IndexedStudy]:
    found: list[IndexedStudy] = []
    for row in rows:
        digest: str | None = None
        unreadable: str | None = None
        try:
            protocol = retained_protocol({"protocol": json.loads(row["protocol"]), "protocol_hash": row["protocol_hash"]})
            digest = setup_sha256(protocol.setup)
        except (TypeError, ValueError) as exc:
            unreadable = f"The frozen protocol cannot be read: {exc}"
        found.append(
            IndexedStudy(
                id=row["id"],
                recorded_name=row["setup_name"],
                underlying=row["underlying"],
                setup_sha256=digest,
                unreadable_reason=unreadable,
                created_at_ms=row["created_at_ms"],
                updated_at_ms=row["updated_at_ms"],
                phase=row["phase"],
                status=row["status"],
            )
        )
    return found


def indexed_approvals(
    records: Sequence[GoldenRecord], events: Sequence[GoldenEvent], *, running_digest: str | None
) -> list[IndexedApproval]:
    by_version: dict[str, list[GoldenEvent]] = {record.id: [] for record in records}
    for event in events:
        by_version[event.qualification_id].append(event)
    found: list[IndexedApproval] = []
    for record in records:
        status, _ = qualification_status(record, by_version[record.id], running_digest=running_digest)
        evidence = record.evidence
        study_id = evidence.get("options_search_study_id")
        found.append(
            IndexedApproval(
                id=record.id,
                recorded_name=record.name,
                underlying=record.setup.get("underlying"),
                setup_sha256=record.setup_sha256,
                source_run_id=record.source_run_id,
                study_id=study_id,
                origin=options_evidence.approval_origin(
                    study_id, evidence.get("study_manifest_sha256"), evidence.get("verdict")
                ),
                status=status,
                created_at_ms=record.created_at_ms,
            )
        )
    return found


async def _read_runs() -> tuple[list[IndexedRun], int]:
    async def read(conn: Any) -> tuple[Any, dict[int, SavedSetup], int]:
        rows = await saved_runs.read_run_rows(conn, limit=INDEX_LIMIT)
        frozen = await saved_runs.read_run_setups(conn, [row["id"] for row in rows])
        return rows, frozen, await saved_runs.count_runs(conn)

    rows, frozen, total = await with_connection(read)
    # A running run whose job is gone reads interrupted; that asks Redis on a worker thread, after the connection is released.
    records = await saved_runs.present_runs(rows, is_live=lifecycle.job_is_live)
    return indexed_runs(records, frozen), total


async def _read_studies() -> tuple[list[IndexedStudy], int]:
    async def read(conn: Any) -> tuple[list[dict[str, Any]], int]:
        return await studies.index_rows(conn, limit=INDEX_LIMIT), await studies.count(conn)

    rows, total = await with_connection(read)
    return indexed_studies(rows), total


async def _read_approvals() -> tuple[list[IndexedApproval], int]:
    store = PostgresGoldenStore()
    records = await store.list(limit=INDEX_LIMIT)
    total = await store.count()
    if not records:
        return [], total
    events = await store.events_for([record.id for record in records])
    try:
        running = await asyncio.to_thread(current_rules_digest)
    except GoldenProofError:
        running = None
    return indexed_approvals(records, events, running_digest=running), total


async def read_index() -> OptionsSetupIndexResponse:
    saved = await with_connection(saved_setups.list_setups)
    runs, runs_total = await _read_runs()
    found_studies, studies_total = await _read_studies()
    approvals, approvals_total = await _read_approvals()
    return OptionsSetupIndexResponse(
        read_at_ms=now_ms_utc(),
        setups=indexed_setups(saved),
        runs=runs,
        runs_total=runs_total,
        studies=found_studies,
        studies_total=studies_total,
        approvals=approvals,
        approvals_total=approvals_total,
    )
