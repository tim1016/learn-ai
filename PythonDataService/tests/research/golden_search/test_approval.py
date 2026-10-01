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
from app.jobs.progress import JobCancelled
from app.lean_sidecar.trading_calendar import expected_sessions
from app.research.backtest_runs import repository as backtest_repo
from app.research.backtest_runs.records import record_from_payload
from app.research.golden_search import approval as approval_module
from app.research.golden_search.approval import (
    PROOF_EVALUATIONS,
    RUN_EVALUATIONS,
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
    revoke_qualification,
)
from app.research.golden_search.zoom import BudgetExhausted
from app.research.golden_validation import repository as golden_repo
from app.research.golden_validation import service as golden_validation
from app.research.persistence.db import run_sync, with_connection
from app.research.sweep.snapshot import DataSnapshot, capture_data_snapshot
from app.schemas.engine_backtest import EngineBacktestRequest, EngineBacktestResponse
from app.schemas.run_admission import QUALIFICATION_REVOKED
from app.services import signal_program_admission as admission_module
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
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
    """The persisted final-interval run: records each request and saves one history row per call.

    ``saved_params`` makes the saved row record other parameters than it was
    asked to run; ``on_run`` fires while the run is in progress.
    """

    def __init__(
        self,
        *,
        save_outcome: str = "saved",
        saved_params: dict[str, Any] | None = None,
        on_run: Callable[[], None] = lambda: None,
    ) -> None:
        self.requests: list[tuple[EngineBacktestRequest, dict[str, str] | None]] = []
        self.save_outcome = save_outcome
        self.saved_params = saved_params or {}
        self.on_run = on_run

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
        self.on_run()
        symbol = str(request.params["symbol"])
        run_id = None
        if self.save_outcome == "saved":
            payload = engine_payload(
                symbol=symbol,
                parameters={**request.params, **self.saved_params},
                program_version=CONTRACT.program_version,
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
        self.cancel_requested = False
        self.reservation_left = PROOF_EVALUATIONS + RUN_EVALUATIONS

    def save_checkpoint(self, checkpoint: ApprovalCheckpoint) -> None:
        # Persisted as the worker would: through its dict form.
        self.checkpoint = ApprovalCheckpoint.from_dict(checkpoint.as_dict())
        self.saved.append(self.checkpoint)

    def consume_reserved(self, evaluations: int) -> None:
        # The study's proof reservation: drawing past it is refused, as the evaluator refuses it.
        if evaluations > self.reservation_left:
            raise BudgetExhausted("the proof reservation is spent")
        self.reservation_left -= evaluations
        self.consumed.append(evaluations)

    def request_cancel(self) -> None:
        self.cancel_requested = True

    def cancel_check(self) -> None:
        if self.cancel_requested:
            raise JobCancelled("cancel requested")

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
        cancel_check=caller.cancel_check,
        blob_store=blobs,
    )


def _start_binding(symbol: str, params: dict[str, Any]) -> BrokerBotBinding:
    """A fresh Start of ``params`` on ``symbol``, as admission resolves its coverage before sealing."""
    return BrokerBotBinding.model_validate(
        {
            "strategy_instance_id": "golden-approval-start",
            "strategy_key": PROGRAM,
            "broker": "alpaca",
            "symbol": symbol,
            "use_rth": True,
            "mode": "log_only",
            "quantity": 1,
            "carryover_policy": "FORBID",
            "action_plan": alpaca_v1_action_plan(symbol.upper()),
            "strategy_params": params,
            "strategy_param_origins": {name: "deploy_override" for name in params},
            "sealed_account_id": "paper-account",
            "run_id": "run-1",
            "created_at_ms": 1,
        }
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


def _commit_then_lose_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The publish commits, then the store fails before its answer arrives (a COMMIT-time timeout)."""
    real_publish = approval_module._publish

    async def publish(conn: asyncpg.Connection, **kwargs: Any) -> str:
        await real_publish(conn, **kwargs)
        raise TimeoutError("the command timed out waiting for COMMIT's answer")

    monkeypatch.setattr(approval_module, "_publish", publish)


async def test_a_publish_whose_answer_was_lost_after_commit_reports_the_version(
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
    monkeypatch.setattr(approval_module, "execute_engine_backtest", _Engine())
    _commit_then_lose_the_answer(monkeypatch)
    caller = _Caller(study_id)

    outcome = await _approve(_request(study_id, symbol, snapshot, lake), caller, blobs)

    # Never "nothing was published" about a version that was.
    assert outcome.status == "approved"
    assert outcome.qualification_id == qualification_id_for(study_id)
    pointer = await get_default(conn, PROGRAM, symbol)
    assert pointer is not None and pointer.qualification_id == outcome.qualification_id
    assert await _study_state(conn, study_id) == "approved"


async def test_a_store_still_down_after_the_publish_never_claims_nothing_was_published(
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
    monkeypatch.setattr(approval_module, "execute_engine_backtest", _Engine())
    _commit_then_lose_the_answer(monkeypatch)
    real_lookup = approval_module.qualifications.get_qualification_by_study
    lookups = 0

    async def lookup_until_the_store_drops(conn: asyncpg.Connection, study: str):
        nonlocal lookups
        lookups += 1
        if lookups > 2:  # the opening check and the one inside the publish succeed; the look afterwards cannot
            raise ConnectionRefusedError("research store is down")
        return await real_lookup(conn, study)

    monkeypatch.setattr(approval_module.qualifications, "get_qualification_by_study", lookup_until_the_store_drops)
    caller = _Caller(study_id)

    outcome = await _approve(_request(study_id, symbol, snapshot, lake), caller, blobs)

    assert outcome.status == "failed"
    assert outcome.failure_code == "STORE_UNAVAILABLE"
    assert "is not known" in (outcome.failure_reason or "")
    assert "Nothing was published" not in (outcome.failure_reason or "")
    # It had in fact committed; a retry once the store is back answers with it.
    monkeypatch.setattr(approval_module.qualifications, "get_qualification_by_study", real_lookup)
    retried = await _approve(_request(study_id, symbol, snapshot, lake), caller, blobs)
    assert (retried.status, retried.qualification_id) == ("approved", qualification_id_for(study_id))


async def test_a_checkpointed_proof_from_other_bytes_is_rebuilt_never_published(
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
    # The proof was built under code that is no longer the running build (a deploy between attempts).
    stale = {**caller.checkpoint.proof, "artifact_digest": "9" * 64}
    caller.checkpoint = dataclasses.replace(caller.checkpoint, proof=stale)
    engine = _Engine()
    monkeypatch.setattr(approval_module, "execute_engine_backtest", engine)

    outcome = await _approve(_request(study_id, symbol, snapshot, lake), caller, blobs)

    # A retry is never stranded on its own checkpoint: the proof is rebuilt under the running build.
    assert outcome.status == "approved"
    row = await get_qualification(conn, outcome.qualification_id or "")
    assert row is not None
    assert row.proof != stale
    assert (row.artifact_digest, row.wiring_digest) == admission_module.running_build_digests(CONTRACT)
    assert ProofRecord.from_dict(row.proof).artifact_digest == row.artifact_digest
    # Rebuilt from the reservation already drawn: nothing new is consumed.
    assert caller.consumed == [2, 1]
    assert len(engine.requests) == 1


async def test_a_checkpointed_proof_whose_inputs_were_lost_is_rebuilt_from_the_lake(
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
    lost = next(iter(ProofRecord.from_dict(caller.checkpoint.proof).manifest.values()))
    (blobs.root / lost[:2] / lost).unlink()
    monkeypatch.setattr(approval_module, "execute_engine_backtest", _Engine())

    outcome = await _approve(_request(study_id, symbol, snapshot, lake), caller, blobs)

    assert outcome.status == "approved"
    row = await get_qualification(conn, outcome.qualification_id or "")
    assert row is not None
    # Every input the published proof names is back in the store, so a later re-proof can replay it.
    for digest in ProofRecord.from_dict(row.proof).manifest.values():
        assert blobs.get(digest)
    assert caller.consumed == [2, 1]


# ---------------------------------------------------------------------------
# Refusals that reach the publish, and before it
# ---------------------------------------------------------------------------
async def test_a_saved_run_that_does_not_record_the_exact_tuple_is_refused_and_rolled_back(
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
    engine = _Engine(saved_params={"rsi_min": 46.0})
    monkeypatch.setattr(approval_module, "execute_engine_backtest", engine)
    caller = _Caller(study_id)

    outcome = await _approve(_request(study_id, symbol, snapshot, lake, expected_default=prior), caller, blobs)

    assert outcome.status == "failed"
    assert outcome.failure_code == "GOLDEN_VALIDATION_REFUSED"
    assert await get_qualification_by_study(conn, study_id) is None
    run_id = caller.checkpoint.run_id
    assert run_id is not None
    # The mismatched run was never left behind as an accepted Golden Validation case.
    assert await golden_repo.get_golden_run_by_source(conn, run_id) is None
    pointer = await get_default(conn, PROGRAM, symbol)
    assert pointer is not None and pointer.qualification_id == prior and pointer.revision == 1
    assert caller.commits == []


async def test_a_cancel_requested_while_the_run_ran_publishes_nothing_and_resumes(
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
    engine = _Engine(on_run=caller.request_cancel)
    monkeypatch.setattr(approval_module, "execute_engine_backtest", engine)

    with pytest.raises(JobCancelled):
        await _approve(_request(study_id, symbol, snapshot, lake), caller, blobs)

    assert await get_qualification_by_study(conn, study_id) is None
    assert await get_default(conn, PROGRAM, symbol) is None
    assert caller.commits == []
    assert await _study_state(conn, study_id) == "qualification_pending"
    assert caller.checkpoint.run_id is not None

    caller.cancel_requested = False
    resumed = await _approve(_request(study_id, symbol, snapshot, lake), caller, blobs)

    assert resumed.status == "approved"
    assert caller.consumed == [2, 1]
    assert len(engine.requests) == 1


async def test_a_spent_reservation_refuses_before_any_proof_or_run(
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
    caller.reservation_left = 0

    outcome = await _approve(_request(study_id, symbol, snapshot, lake), caller, blobs)

    assert outcome.status == "failed"
    assert outcome.failure_code == "BUDGET_EXHAUSTED"
    assert caller.saved == []
    assert not blobs.root.exists() or not any(blobs.root.iterdir())
    assert engine.requests == []
    assert await get_qualification_by_study(conn, study_id) is None


async def test_a_candidate_not_in_its_canonical_form_is_never_published_under_another_identity(
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
    # The canonical dump omits an identity-neutral default; spelling it out is another parameter hash.
    spelled_out = dataclasses.replace(
        _request(study_id, symbol, snapshot, lake), candidate_point={**_candidate(symbol), "fast_period": 5}
    )

    outcome = await _approve(spelled_out, caller, blobs)

    assert outcome.status == "failed"
    assert outcome.failure_code == "PARAMETERS_CHANGED"
    assert caller.consumed == []
    assert engine.requests == []
    assert await get_qualification_by_study(conn, study_id) is None


async def test_the_published_version_covers_its_tuple_at_start_until_it_is_revoked(
    conn: asyncpg.Connection,
    unique: str,
    symbol: str,
    snapshot: DataSnapshot,
    lake: Path,
    blobs: BlobStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seam between approval and admission, through the real store: what one writes, the other finds."""
    study_id = f"study-{unique}"
    await _study(conn, study_id, symbol)
    monkeypatch.setattr(approval_module, "execute_engine_backtest", _Engine())
    approved = await _approve(_request(study_id, symbol, snapshot, lake), _Caller(study_id), blobs)
    assert approved.status == "approved"
    start = _start_binding(symbol.lower(), {k: v for k, v in _candidate(symbol).items() if k != "symbol"})

    covered = await admission_module.resolve_admission_coverage(start)
    await revoke_qualification(
        conn,
        qualification_id=approved.qualification_id or "",
        reason="Withdrawn after review.",
        actor="local:owner",
        command_id=f"revoke-{unique}",
        now_ms=2,
    )
    revoked = await admission_module.resolve_admission_coverage(start)

    assert covered is not None
    assert (covered.state, covered.qualification_id) == ("COVERED", approved.qualification_id)
    assert revoked is not None
    assert (revoked.state, revoked.qualification_id, revoked.explanation) == (
        "UNCOVERED",
        None,
        QUALIFICATION_REVOKED,
    )


def test_approval_checkpoint_round_trips_through_its_dict_form() -> None:
    checkpoint = ApprovalCheckpoint(run_id=7, proof={"schema_version": 1}, proof_reserved=True, run_reserved=True)

    assert ApprovalCheckpoint.from_dict(checkpoint.as_dict()) == checkpoint
    assert ApprovalCheckpoint.from_dict(None) == ApprovalCheckpoint()
    with pytest.raises(ValueError, match="run_id"):
        ApprovalCheckpoint.from_dict({"run_id": "7"})
