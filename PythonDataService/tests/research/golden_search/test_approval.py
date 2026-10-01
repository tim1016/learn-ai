"""Approving a Golden Search study publishes one exact qualified version, or nothing (#2696).

Live Postgres only (``POSTGRES_URL_IS_EPHEMERAL=1``). The proof is real: the
registered EMA program replays a seeded, catalog-admitted adjusted lake twice,
as in ``test_proof.py``. The persisted final-interval run is the one injected
seam — a stand-in for the engine that records the request it was given and
saves a history row the way the engine would — so these tests exercise the
Golden Validation designation and review, the qualification insert and the
default compare-and-set against the real tables. Each test owns a stock of its
own, so the per-stock default pointer is never shared between parallel tests.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from app.data_lake.path_policy import lake_subpath
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.lean_sidecar.trading_calendar import expected_sessions
from app.research.backtest_runs import repository as backtest_repo
from app.research.backtest_runs.records import record_from_payload
from app.research.golden_search import approval as approval_module
from app.research.golden_search.approval import (
    ApprovalCheckpoint,
    ApprovalOutcome,
    ApprovalRequest,
    approve_study,
    qualification_id_for,
)
from app.research.golden_search.proof import BlobStore, ProofRecord, ProofWindow
from app.research.golden_search.protocol import ExecutionAssumptions
from app.research.golden_search.qualifications import (
    get_default,
    get_qualification,
    get_qualification_by_study,
)
from app.research.golden_validation import repository as golden_repo
from app.research.golden_validation import service as golden_validation
from app.research.persistence.db import run_sync, with_connection
from app.research.sweep.snapshot import DataSnapshot, capture_data_snapshot
from app.schemas.engine_backtest import EngineBacktestRequest, EngineBacktestResponse
from app.services import signal_program_admission as admission_module
from app.utils.session_anchors import et_midnight_ms
from tests._helpers.golden_qualification import canonical_point, seed_qualification
from tests._helpers.lean_store import seed_store_day
from tests.research.backtest_runs.payloads import engine_payload
from tests.services.test_signal_program_admission import _copied_source_tree

pytestmark = pytest.mark.usefixtures("seeded_lake_catalog")

PROGRAM = "ema_crossover_signal"
REGISTRATION = _STRATEGY_REGISTRY[PROGRAM]
CONTRACT = REGISTRATION.signal_program_contract
assert CONTRACT is not None
SNAPSHOT_START, SNAPSHOT_END = date(2025, 2, 3), date(2025, 2, 14)
WINDOW = ProofWindow(
    start_ms=et_midnight_ms(date(2025, 2, 4)),
    end_ms=et_midnight_ms(date(2025, 2, 7)),
    warmup_from_ms=et_midnight_ms(date(2025, 2, 3)),
)
NOTE = "Approved after the final test; the research weakness is accepted."


@pytest.fixture
def symbol(unique: str) -> str:
    return f"GA{unique.upper()}"


@pytest.fixture
def lake(tmp_path: Path, symbol: str) -> Path:
    root = tmp_path / "writer-root" / lake_subpath("polygon_split_adjusted")
    root.mkdir(parents=True)
    for day in expected_sessions(SNAPSHOT_START, SNAPSHOT_END):
        seed_store_day(root, symbol, day)
    return root


@pytest.fixture
def snapshot(lake: Path, symbol: str) -> DataSnapshot:
    return capture_data_snapshot(
        roots=[lake], symbol=symbol, resolution="minute", data_start=SNAPSHOT_START, data_end=SNAPSHOT_END
    )


@pytest.fixture
def blobs(tmp_path: Path) -> BlobStore:
    return BlobStore(tmp_path / "blobs")


def _candidate(symbol: str) -> dict[str, Any]:
    return REGISTRATION.param_schema.model_validate(
        {**CONTRACT.validated_settings, "rsi_min": 45.0, "symbol": symbol}
    ).model_dump(mode="json")


async def _study(conn: asyncpg.Connection, study_id: str, symbol: str) -> None:
    await conn.execute(
        """
        INSERT INTO research_golden_search_studies (
            id, strategy_key, symbol, state, created_at_ms, updated_at_ms,
            protocol_json, protocol_hash, receipt_json, budget_cap
        ) VALUES ($1, $2, $3, 'qualification_pending', 1, 1, '{}'::jsonb, $4, '{}'::jsonb, 100)
        """,
        study_id,
        PROGRAM,
        symbol,
        "h" * 64,
    )


class _Engine:
    """The persisted final-interval run: records each request and saves one history row per call."""

    def __init__(self, *, save_outcome: str = "saved") -> None:
        self.requests: list[tuple[EngineBacktestRequest, dict[str, str] | None]] = []
        self.save_outcome = save_outcome

    def __call__(
        self,
        *,
        request: EngineBacktestRequest,
        on_phase: Callable[[str], None],
        on_log: Callable[[str], None],
        data_manifest: dict[str, str] | None = None,
        while_waiting: Callable[[], None] = lambda: None,
    ) -> EngineBacktestResponse:
        self.requests.append((request, None if data_manifest is None else dict(data_manifest)))
        symbol = str(request.params["symbol"])
        run_id = None
        if self.save_outcome == "saved":
            payload = engine_payload(
                symbol=symbol, parameters=dict(request.params), program_version=CONTRACT.program_version
            )
            run_id = run_sync(with_connection(backtest_repo.insert_run, record_from_payload(payload))).run_id
        return EngineBacktestResponse.model_validate(
            {
                "success": True,
                "strategy_name": request.strategy_name,
                "fill_mode": request.fill_mode,
                "initial_cash": 100_000.0,
                "final_equity": 100_000.0,
                "net_profit": 0.0,
                "total_fees": 0.0,
                "total_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "win_rate": 0.0,
                "study_id": run_id,
                "save_outcome": self.save_outcome,
            }
        )


class _Caller:
    """What the study's job worker hands approval: checkpoint storage, the reservation, the fenced update."""

    def __init__(self, conn_study_id: str) -> None:
        self.study_id = conn_study_id
        self.checkpoint = ApprovalCheckpoint()
        self.saved: list[ApprovalCheckpoint] = []
        self.consumed: list[int] = []
        self.commits: list[tuple[bool, str]] = []
        self.fail_commit = False

    def save_checkpoint(self, checkpoint: ApprovalCheckpoint) -> None:
        # Persisted as the worker would: through its dict form.
        self.checkpoint = ApprovalCheckpoint.from_dict(checkpoint.as_dict())
        self.saved.append(self.checkpoint)

    def consume_reserved(self, evaluations: int) -> None:
        self.consumed.append(evaluations)

    async def on_commit(self, conn: asyncpg.Connection, qualification_id: str) -> None:
        self.commits.append((conn.is_in_transaction(), qualification_id))
        await conn.execute(
            "UPDATE research_golden_search_studies SET state = 'approved', updated_at_ms = 2 WHERE id = $1",
            self.study_id,
        )
        if self.fail_commit:
            raise RuntimeError("the study's fence moved")


def _request(
    study_id: str, symbol: str, snapshot: DataSnapshot, lake: Path, *, expected_default: str | None = None
) -> ApprovalRequest:
    return ApprovalRequest(
        study_id=study_id,
        strategy_key=PROGRAM,
        symbol=symbol,
        candidate_point=_candidate(symbol),
        proof_window=WINDOW,
        snapshot=snapshot,
        roots=(lake,),
        execution=ExecutionAssumptions(),
        research={"exam_outcome": "not_enough_evidence", "claim": "exploratory", "research_override": True},
        note=NOTE,
        expected_default_qualification_id=expected_default,
        actor="local:owner",
    )


async def _approve(request: ApprovalRequest, caller: _Caller, blobs: BlobStore) -> ApprovalOutcome:
    # Approval runs on the job worker thread: the lake read refuses a running event loop.
    return await asyncio.to_thread(
        approve_study,
        request,
        checkpoint=caller.checkpoint,
        save_checkpoint=caller.save_checkpoint,
        consume_reserved=caller.consume_reserved,
        on_commit=caller.on_commit,
        blob_store=blobs,
    )


async def _prior_default(conn: asyncpg.Connection, symbol: str, unique: str) -> str:
    """An earlier qualified version already holding the stock's default."""
    prior = await seed_qualification(
        conn,
        qualification_id=f"gq-prior-{unique}",
        symbol=symbol,
        params=canonical_point(symbol, rsi_min=40.0),
        artifact_digest="1" * 64,
        make_default=True,
    )
    return prior.id


async def _study_state(conn: asyncpg.Connection, study_id: str) -> str:
    return await conn.fetchval("SELECT state FROM research_golden_search_studies WHERE id = $1", study_id)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
async def test_approve_study_publishes_one_qualified_version_and_moves_the_default(
    conn: asyncpg.Connection,
    unique: str,
    symbol: str,
    snapshot: DataSnapshot,
    lake: Path,
    blobs: BlobStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    study_id = f"study-{unique}"
    await _study(conn, study_id, symbol)
    engine = _Engine()
    monkeypatch.setattr(approval_module, "execute_engine_backtest", engine)
    caller = _Caller(study_id)

    outcome = await _approve(_request(study_id, symbol, snapshot, lake), caller, blobs)

    qualification_id = qualification_id_for(study_id)
    assert outcome == ApprovalOutcome(
        status="approved", qualification_id=qualification_id, failure_code=None, failure_reason=None
    )
    row = await get_qualification(conn, qualification_id)
    assert row is not None
    assert row.study_id == study_id
    assert row.symbol == symbol
    assert row.params == _candidate(symbol)
    assert row.program_version == CONTRACT.program_version
    assert row.note == NOTE
    assert row.research["research_override"] is True
    proof = ProofRecord.from_dict(row.proof)
    assert proof.sha256() == row.proof_sha256
    assert proof.params == _candidate(symbol)
    assert proof.window == WINDOW
    assert (row.artifact_digest, row.wiring_digest) == (proof.artifact_digest, proof.wiring_digest)
    assert (row.artifact_digest, row.wiring_digest) == admission_module.running_build_digests(CONTRACT)
    # Golden Validation: the persisted run is the case, accepted as a Manual override with the owner's note.
    dossier = await golden_validation.get_dossier(conn, row.golden_run_id)
    assert dossier is not None
    assert dossier.golden_run.source_run_id == caller.checkpoint.run_id
    assert dossier.validation_case["parameters"] == _candidate(symbol)
    assert dossier.latest_review is not None and dossier.latest_review.id == row.golden_review_id
    assert dossier.latest_review.decision == "accept"
    assert dossier.latest_review.classification == "manual_override"
    assert dossier.latest_review.reason == NOTE
    # The default and the study moved in the same transaction.
    pointer = await get_default(conn, PROGRAM, symbol)
    assert pointer is not None and pointer.qualification_id == qualification_id and pointer.revision == 1
    assert caller.commits == [(True, qualification_id)]
    assert await _study_state(conn, study_id) == "approved"
    # Exactly the reserved budget: two proof replays, one persisted run.
    assert caller.consumed == [2, 1]
    assert caller.checkpoint.proof is not None and caller.checkpoint.run_id is not None
    # The persisted run is Python-only, full, saved, and bound to the study's data receipt.
    ((engine_request, manifest),) = engine.requests
    assert engine_request.requested_engine == "python"
    assert engine_request.save_study is True
    assert engine_request.summary_only is False
    assert engine_request.auto_fetch is False
    assert (engine_request.from_date, engine_request.to_date, engine_request.warmup_from_date) == (
        "2025-02-04",
        "2025-02-06",
        "2025-02-03",
    )
    assert engine_request.params == _candidate(symbol)
    assert manifest == snapshot.artifacts


async def test_approve_study_after_a_lost_response_answers_with_the_published_version(
    conn: asyncpg.Connection,
    unique: str,
    symbol: str,
    snapshot: DataSnapshot,
    lake: Path,
    blobs: BlobStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    study_id = f"study-{unique}"
    await _study(conn, study_id, symbol)
    engine = _Engine()
    monkeypatch.setattr(approval_module, "execute_engine_backtest", engine)
    caller = _Caller(study_id)
    first = await _approve(_request(study_id, symbol, snapshot, lake), caller, blobs)

    again = await _approve(_request(study_id, symbol, snapshot, lake), caller, blobs)

    assert again == first
    assert caller.consumed == [2, 1]
    assert len(engine.requests) == 1
    assert len(caller.commits) == 1


# ---------------------------------------------------------------------------
# Technical failures publish nothing and keep the prior default
# ---------------------------------------------------------------------------
async def test_proof_mismatch_publishes_nothing_and_keeps_the_default(
    conn: asyncpg.Connection,
    unique: str,
    symbol: str,
    snapshot: DataSnapshot,
    lake: Path,
    blobs: BlobStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    study_id = f"study-{unique}"
    await _study(conn, study_id, symbol)
    prior = await _prior_default(conn, symbol, unique)
    engine = _Engine()
    monkeypatch.setattr(approval_module, "execute_engine_backtest", engine)
    seed_store_day(lake, symbol, date(2025, 2, 5), count=200)  # bytes moved since the study's receipt
    caller = _Caller(study_id)

    outcome = await _approve(_request(study_id, symbol, snapshot, lake, expected_default=prior), caller, blobs)

    assert outcome.status == "failed"
    assert outcome.failure_code == "PROOF_MISMATCH"
    assert "RECEIPT_MISMATCH" in (outcome.failure_reason or "")
    assert "current default is unchanged" in (outcome.failure_reason or "")
    assert await get_qualification_by_study(conn, study_id) is None
    pointer = await get_default(conn, PROGRAM, symbol)
    assert pointer is not None and pointer.qualification_id == prior and pointer.revision == 1
    assert engine.requests == []
    assert caller.commits == []
    assert await _study_state(conn, study_id) == "qualification_pending"


async def test_a_moved_default_refuses_and_rolls_back_the_review_then_a_fresh_review_publishes(
    conn: asyncpg.Connection,
    unique: str,
    symbol: str,
    snapshot: DataSnapshot,
    lake: Path,
    blobs: BlobStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    study_id = f"study-{unique}"
    await _study(conn, study_id, symbol)
    prior = await _prior_default(conn, symbol, unique)
    engine = _Engine()
    monkeypatch.setattr(approval_module, "execute_engine_backtest", engine)
    caller = _Caller(study_id)

    # The owner reviewed when no default existed; another approval moved it since.
    refused = await _approve(_request(study_id, symbol, snapshot, lake, expected_default=None), caller, blobs)

    assert refused.status == "failed"
    assert refused.failure_code == "DEFAULT_CHANGED"
    assert prior in (refused.failure_reason or "")
    assert await get_qualification_by_study(conn, study_id) is None
    run_id = caller.checkpoint.run_id
    assert run_id is not None
    assert await golden_repo.get_golden_run_by_source(conn, run_id) is None  # designation rolled back too
    pointer = await get_default(conn, PROGRAM, symbol)
    assert pointer is not None and pointer.qualification_id == prior
    assert caller.commits == []

    approved = await _approve(_request(study_id, symbol, snapshot, lake, expected_default=prior), caller, blobs)

    assert approved.status == "approved"
    pointer = await get_default(conn, PROGRAM, symbol)
    assert pointer is not None and pointer.qualification_id == approved.qualification_id and pointer.revision == 2
    # The retry reused the proof and the run: no second draw on the reservation, no second run.
    assert caller.consumed == [2, 1]
    assert len(engine.requests) == 1


async def test_restart_needed_refuses_before_any_work(
    conn: asyncpg.Connection,
    unique: str,
    symbol: str,
    snapshot: DataSnapshot,
    lake: Path,
    blobs: BlobStore,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    study_id = f"study-{unique}"
    await _study(conn, study_id, symbol)
    engine = _Engine()
    monkeypatch.setattr(approval_module, "execute_engine_backtest", engine)
    # A pull landed on disk after this process imported the program.
    monkeypatch.setattr(admission_module, "_SERVICE_ROOT", _copied_source_tree(tmp_path / "src", drift_artifact=True))
    caller = _Caller(study_id)

    outcome = await _approve(_request(study_id, symbol, snapshot, lake), caller, blobs)

    assert outcome.status == "failed"
    assert outcome.failure_code == "RESTART_NEEDED"
    assert "Restart the service" in (outcome.failure_reason or "")
    assert caller.consumed == []
    assert caller.saved == []
    assert engine.requests == []
    assert await get_qualification_by_study(conn, study_id) is None


# ---------------------------------------------------------------------------
# Resuming after a failure between steps
# ---------------------------------------------------------------------------
async def test_a_run_that_was_not_saved_resumes_from_the_proof_without_drawing_again(
    conn: asyncpg.Connection,
    unique: str,
    symbol: str,
    snapshot: DataSnapshot,
    lake: Path,
    blobs: BlobStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    study_id = f"study-{unique}"
    await _study(conn, study_id, symbol)
    caller = _Caller(study_id)
    monkeypatch.setattr(approval_module, "execute_engine_backtest", _Engine(save_outcome="failed"))

    failed = await _approve(_request(study_id, symbol, snapshot, lake), caller, blobs)

    assert failed.failure_code == "RUN_NOT_SAVED"
    assert caller.checkpoint.proof is not None and caller.checkpoint.run_id is None
    assert caller.checkpoint.run_reserved is True
    built = caller.checkpoint.proof

    def _no_rebuild(**_kwargs: object) -> ProofRecord:
        raise AssertionError("a checkpointed proof must be reused, not rebuilt")

    monkeypatch.setattr(approval_module, "build_proof", _no_rebuild)
    engine = _Engine()
    monkeypatch.setattr(approval_module, "execute_engine_backtest", engine)

    approved = await _approve(_request(study_id, symbol, snapshot, lake), caller, blobs)

    assert approved.status == "approved"
    assert caller.consumed == [2, 1]
    assert len(engine.requests) == 1
    row = await get_qualification(conn, approved.qualification_id or "")
    assert row is not None and row.proof == built


async def test_a_publish_that_rolled_back_resumes_without_a_new_proof_or_run(
    conn: asyncpg.Connection,
    unique: str,
    symbol: str,
    snapshot: DataSnapshot,
    lake: Path,
    blobs: BlobStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    study_id = f"study-{unique}"
    await _study(conn, study_id, symbol)
    engine = _Engine()
    monkeypatch.setattr(approval_module, "execute_engine_backtest", engine)
    caller = _Caller(study_id)
    caller.fail_commit = True

    failed = await _approve(_request(study_id, symbol, snapshot, lake), caller, blobs)

    assert failed.status == "failed"
    assert failed.failure_code == "APPROVAL_ERROR"
    # The study update ran inside the transaction and rolled back with it.
    assert await _study_state(conn, study_id) == "qualification_pending"
    assert await get_qualification_by_study(conn, study_id) is None
    assert await get_default(conn, PROGRAM, symbol) is None

    caller.fail_commit = False
    approved = await _approve(_request(study_id, symbol, snapshot, lake), caller, blobs)

    assert approved.status == "approved"
    assert caller.consumed == [2, 1]
    assert len(engine.requests) == 1
    assert await _study_state(conn, study_id) == "approved"


async def test_a_checkpointed_proof_from_other_bytes_is_never_published(
    conn: asyncpg.Connection,
    unique: str,
    symbol: str,
    snapshot: DataSnapshot,
    lake: Path,
    blobs: BlobStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    study_id = f"study-{unique}"
    await _study(conn, study_id, symbol)
    monkeypatch.setattr(approval_module, "execute_engine_backtest", _Engine(save_outcome="failed"))
    caller = _Caller(study_id)
    await _approve(_request(study_id, symbol, snapshot, lake), caller, blobs)
    assert caller.checkpoint.proof is not None
    # The proof was built under code that is no longer the running build.
    caller.checkpoint = dataclasses.replace(
        caller.checkpoint, proof={**caller.checkpoint.proof, "artifact_digest": "9" * 64}
    )
    engine = _Engine()
    monkeypatch.setattr(approval_module, "execute_engine_backtest", engine)

    outcome = await _approve(_request(study_id, symbol, snapshot, lake), caller, blobs)

    assert outcome.status == "failed"
    assert outcome.failure_code == "PROOF_STALE"
    assert engine.requests == []
    assert await get_qualification_by_study(conn, study_id) is None


def test_approval_checkpoint_round_trips_through_its_dict_form() -> None:
    checkpoint = ApprovalCheckpoint(run_id=7, proof={"schema_version": 1}, proof_reserved=True, run_reserved=True)

    assert ApprovalCheckpoint.from_dict(checkpoint.as_dict()) == checkpoint
    assert ApprovalCheckpoint.from_dict(None) == ApprovalCheckpoint()
    with pytest.raises(ValueError, match="run_id"):
        ApprovalCheckpoint.from_dict({"run_id": "7"})
