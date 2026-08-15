"""Direct ledger coverage for SQLite execution-slice custody (#1441 S1.2).

The S0 golden families include economic projections that S2 has not built yet.
These tests deliberately replay their execution facts through the S1 ledger
only: identity, attribution, effective correction, recovery, and mirror
rebuild are all observable without fabricating an S2 P&L projection.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.broker.alpaca.adapter import from_alpaca_trade_update
from app.broker.alpaca.clerk.sqlite import repository as repository_module
from app.broker.alpaca.clerk.sqlite import repository_execution_coverage_api as coverage_api_module
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.enter import EnterSubmission, accept_enter
from app.broker.alpaca.clerk.sqlite.execution_coverage import ExecutionCoverageSupersededFacts
from app.broker.alpaca.clerk.sqlite.facts import (
    ExecutionCorrectedFacts,
    ExecutionCoverageQuarantinedFacts,
    ExecutionCoverageResolvedFacts,
    ExecutionSliceFilledFacts,
    UncertaintyRaisedFacts,
)
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
from app.broker.alpaca.clerk.sqlite.projections import SqliteClerkProjectionReader
from app.broker.alpaca.clerk.sqlite.recovery_execution import (
    RecoveryExecutionRequest,
    execute_recovery_action,
)
from app.broker.alpaca.clerk.sqlite.recovery_policy import (
    RecoveryPolicyContext,
    build_recovery_catalog,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.repository_execution_coverage_api import (
    ExecutionCoverageResolutionUnavailable,
)
from app.broker.alpaca.clerk.sqlite.uncertainty import Capability, decide_capability
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
    ExecutionCoverageConflictCause,
)
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg
from tests.broker.alpaca.clerk.sqlite.conftest import _clock_at

_FIXTURE_ROOT = Path(__file__).parents[4] / "fixtures" / "golden" / "alpaca-sqlite-execution"
_ACCOUNT_ID = "PA-S1-EXECUTION"
_RUN_ID = "run-s1"
_ATOL = 1e-9


def _load_fixture(family: str) -> tuple[dict, dict]:
    fixture_dir = _FIXTURE_ROOT / family
    return (
        json.loads((fixture_dir / "input.json").read_text(encoding="utf-8")),
        json.loads((fixture_dir / "expected.json").read_text(encoding="utf-8")),
    )


def _repository_for_strategy(
    tmp_path: Path,
    *,
    strategy_instance_id: str,
    symbol: str,
) -> tuple[ClerkSqliteRepository, EnterSubmission]:
    clock = _clock_at(1_786_368_000_000)
    repo = ClerkSqliteRepository.initialize(
        account_id=_ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=clock,
    )
    repo.register_strategy_instance(
        strategy_instance_id=strategy_instance_id,
        symbol=symbol,
        config_hash="s1-ledger-config",
    )
    submit_start_run(
        repo,
        account_id=_ACCOUNT_ID,
        strategy_instance_id=strategy_instance_id,
        lifecycle_run_id=_RUN_ID,
    )
    accepted = accept_enter(
        repo,
        account_id=_ACCOUNT_ID,
        strategy_instance_id=strategy_instance_id,
        decision_id="fixture-execution",
        lifecycle_run_id=_RUN_ID,
        leg=BrokerOrderLeg(symbol=symbol, side="buy", quantity=10),
    )
    assert accepted.effect_operation_id is not None
    assert accepted.order_ref is not None
    return repo, accepted


def _slice_transition(
    repo: ClerkSqliteRepository,
    *,
    accepted: EnterSubmission,
    facts: ExecutionSliceFilledFacts,
) -> TransitionInput:
    return TransitionInput(
        strategy_instance_id=accepted.command.strategy_instance_id,
        run_id=accepted.command.run_id,
        command_id=accepted.command.command_id,
        effect_operation_id=accepted.effect_operation_id,
        order_ref=accepted.order_ref,
        transition_kind="EXECUTION_SLICE_FILLED",
        custody_owner="ACCOUNT_CLERK",
        execution_authority="ACCOUNT_CLERK",
        operation_state="in_progress",
        source_event_at_ms=facts.source_event_at_ms,
        clerk_observed_at_ms=repo.clock(),
        summary_code="EXECUTION_SLICE_FILLED",
        facts_json=facts.to_facts_json(),
    )


def _coverage_conflict_transition(
    repo: ClerkSqliteRepository,
    *,
    accepted: EnterSubmission,
    facts: ExecutionSliceFilledFacts,
) -> TransitionInput:
    conflict_facts = UncertaintyRaisedFacts(
        severity="error",
        blocks_new_exposure=True,
        allows_reduction=False,
        reason_code=EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
        headline="Exact execution overlaps aggregate recovery evidence",
        explanation=(
            "A late websocket execution cannot be safely matched to the order's "
            "previously recorded cumulative-recovery fill."
        ),
        operator_impact="New exposure is blocked until the coverage is reconciled.",
        next_step="Reconcile the broker order and execution slices before resuming.",
        evidence_refs=[facts.execution_id],
        cause_facts=ExecutionCoverageConflictCause(
            order_ref=accepted.order_ref or "",
            execution_id=facts.execution_id,
        ).to_mapping(),
    )
    return TransitionInput(
        strategy_instance_id=accepted.command.strategy_instance_id,
        run_id=accepted.command.run_id,
        command_id=accepted.command.command_id,
        effect_operation_id=accepted.effect_operation_id,
        order_ref=accepted.order_ref,
        transition_kind="UNCERTAINTY_RAISED",
        custody_owner="ACCOUNT_CLERK",
        execution_authority="ACCOUNT_CLERK",
        operation_state="succeeded",
        clerk_observed_at_ms=repo.clock(),
        summary_code=EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
        facts_json=conflict_facts.to_facts_json(),
    )


def _append_slice(
    repo: ClerkSqliteRepository,
    *,
    accepted: EnterSubmission,
    facts: ExecutionSliceFilledFacts,
) -> str:

    return repo.append_execution_slice_if_absent(
        execution_id=facts.execution_id,
        order_ref=accepted.order_ref or "",
        build_transition=lambda: _slice_transition(repo, accepted=accepted, facts=facts),
        build_coverage_conflict=lambda: _coverage_conflict_transition(
            repo,
            accepted=accepted,
            facts=facts,
        ),
    )


def _correction_transition(
    repo: ClerkSqliteRepository,
    *,
    accepted: EnterSubmission,
    facts: ExecutionCorrectedFacts,
    source_event_at_ms: int,
) -> TransitionInput:
    return TransitionInput(
        strategy_instance_id=accepted.command.strategy_instance_id,
        run_id=accepted.command.run_id,
        command_id=accepted.command.command_id,
        effect_operation_id=accepted.effect_operation_id,
        order_ref=accepted.order_ref,
        transition_kind="EXECUTION_CORRECTED",
        custody_owner="ACCOUNT_CLERK",
        execution_authority="ACCOUNT_CLERK",
        operation_state="in_progress",
        source_event_at_ms=source_event_at_ms,
        clerk_observed_at_ms=repo.clock(),
        summary_code="EXECUTION_CORRECTED",
        facts_json=facts.to_facts_json(),
    )


def _correction_uncertainty_transition(
    repo: ClerkSqliteRepository,
    *,
    reason: str,
    execution_id: str,
) -> TransitionInput:
    facts = UncertaintyRaisedFacts(
        severity="error",
        blocks_new_exposure=True,
        allows_reduction=False,
        reason_code="EXECUTION_CORRECTION_UNEXPLAINED",
        headline="An execution correction could not be attributed",
        explanation=reason,
        operator_impact="New exposure is blocked until the correction is reconciled.",
        next_step="Reconcile the referenced broker execution before resuming.",
        evidence_refs=[execution_id],
        cause_facts={"execution_id": execution_id},
    )
    return TransitionInput(
        transition_kind="UNCERTAINTY_RAISED",
        custody_owner="ACCOUNT_CLERK",
        execution_authority="ACCOUNT_CLERK",
        operation_state="succeeded",
        clerk_observed_at_ms=repo.clock(),
        summary_code="UNCERTAINTY_RAISED",
        facts_json=facts.to_facts_json(),
    )


def _append_cumulative_recovery(
    repo: ClerkSqliteRepository,
    *,
    accepted: EnterSubmission,
    cumulative_filled_quantity: float,
    average_price: float,
    source_event_at_ms: int,
) -> None:
    order = BrokerOrder(
        broker="alpaca",
        order_id=f"broker-{accepted.order_ref}",
        client_order_id=accepted.order_ref,
        symbol="SPY",
        asset_class="us_equity",
        side="buy",
        order_type="market",
        time_in_force="day",
        quantity=cumulative_filled_quantity,
        filled_quantity=cumulative_filled_quantity,
        limit_price=None,
        stop_price=None,
        filled_avg_price=average_price,
        status="filled",
        submitted_at_ms=source_event_at_ms - 1,
        created_at_ms=source_event_at_ms - 1,
        updated_at_ms=source_event_at_ms,
        filled_at_ms=source_event_at_ms,
        canceled_at_ms=None,
        expired_at_ms=None,
        events=[],
        observed_at_ms=source_event_at_ms,
    )
    fold_order_evidence(
        repo,
        effect_operation_id=accepted.effect_operation_id or "",
        order=order,
    )


def test_execution_slice_filled_records_exact_slices(tmp_path: Path) -> None:
    fixture, expected = _load_fixture("partial_fill_sequence")
    strategy = fixture["strategy_instance"]
    test_strategy_instance_id = "s1-partial"
    repo, accepted = _repository_for_strategy(
        tmp_path,
        strategy_instance_id=test_strategy_instance_id,
        symbol=strategy["symbol"],
    )
    try:
        for frame in fixture["trade_updates"]:
            payload = frame["data"]
            event = from_alpaca_trade_update(payload)
            facts = ExecutionSliceFilledFacts(
                execution_id=event.execution_id or "",
                symbol=payload["order"]["symbol"],
                side=payload["order"]["side"].upper(),
                slice_qty=event.quantity or 0.0,
                slice_price=event.price or 0.0,
                fee=None,
                fee_fidelity="not_reported",
                evidence_source="websocket",
                source_event_at_ms=event.occurred_at_ms,
            )
            assert _append_slice(repo, accepted=accepted, facts=facts) == "appended"

        fills = repo.fills_for_order(accepted.order_ref or "")
        assert [(fill["fill_id"], fill["execution_id"], fill["qty"], fill["price"]) for fill in fills] == [
            (
                item["fill_id"],
                item["execution_id"],
                item["quantity"],
                item["price"],
            )
            for item in expected["projection"]["fills"]
        ]
        assert all(fill["evidence_source"] == "websocket" for fill in fills)
        assert repo.position(test_strategy_instance_id, strategy["symbol"]) == pytest.approx(
            expected["projection"]["bot_metrics"][strategy["strategy_instance_id"]]["position_quantity"],
            abs=_ATOL,
            rel=0,
        )
    finally:
        repo.close()


def test_execution_correction_replaces_effective_quantity(tmp_path: Path) -> None:
    fixture, expected = _load_fixture("downward_correction")
    strategy = fixture["strategy_instance"]
    test_strategy_instance_id = "s1-correction"
    repo, accepted = _repository_for_strategy(
        tmp_path,
        strategy_instance_id=test_strategy_instance_id,
        symbol=strategy["symbol"],
    )
    repo_closed = False
    try:
        original_payload = fixture["trade_updates"][0]["data"]
        original_event = from_alpaca_trade_update(original_payload)
        original = ExecutionSliceFilledFacts(
            execution_id=original_event.execution_id or "",
            symbol="SPY",
            side="BUY",
            slice_qty=original_event.quantity or 0.0,
            slice_price=original_event.price or 0.0,
            fee=None,
            fee_fidelity="not_reported",
            evidence_source="websocket",
            source_event_at_ms=original_event.occurred_at_ms,
        )
        assert _append_slice(repo, accepted=accepted, facts=original) == "appended"

        correction_payload = fixture["authority_corrections"][0]
        correction = ExecutionCorrectedFacts(
            execution_id=correction_payload["execution_id"],
            superseded_execution_ref=correction_payload["superseded_execution_ref"],
            symbol=correction_payload["symbol"],
            side=correction_payload["side"],
            corrected_qty=correction_payload["corrected_qty"],
            corrected_price=correction_payload["corrected_price"],
            why=correction_payload["why"],
        )
        outcome = repo.append_execution_correction_or_raise(
            correction=_correction_transition(
                repo,
                accepted=accepted,
                facts=correction,
                source_event_at_ms=original_event.occurred_at_ms + 1,
            ),
            build_uncertainty=lambda reason: _correction_uncertainty_transition(
                repo,
                reason=reason,
                execution_id=correction.execution_id,
            ),
        )
        assert outcome == "appended"

        fills = repo.fills_for_order(accepted.order_ref or "")
        assert [(fill["execution_id"], fill["event_kind"], fill["superseded_execution_ref"]) for fill in fills] == [
            ("spy-original-slice-5", "fill", None),
            ("spy-corrected-slice-3", "correction", "spy-original-slice-5"),
        ]
        assert repo.position(test_strategy_instance_id, strategy["symbol"]) == pytest.approx(
            expected["projection"]["bot_metrics"][strategy["strategy_instance_id"]]["position_quantity"],
            abs=_ATOL,
            rel=0,
        )

        original_transitions = repo.custody_transitions()
        original_fills = fills
        db_path = repo.db_path
        repo.close()
        repo_closed = True
        db_path.rename(db_path.with_suffix(".db.corrupt"))
        rebuilt = ClerkSqliteRepository.rebuild_from_mirror(
            account_id=_ACCOUNT_ID,
            artifacts_root=tmp_path,
            clock=_clock_at(1_786_368_000_000),
        )
        try:
            assert rebuilt.custody_transitions() == original_transitions
            assert rebuilt.fills_for_order(accepted.order_ref or "") == original_fills
            assert rebuilt.position(test_strategy_instance_id, strategy["symbol"]) == pytest.approx(
                3.0,
                abs=_ATOL,
                rel=0,
            )
        finally:
            rebuilt.close()
    finally:
        if not repo_closed:
            repo.close()


def test_execution_slice_redelivery_after_restart_adds_no_transition(tmp_path: Path) -> None:
    repo, accepted = _repository_for_strategy(
        tmp_path,
        strategy_instance_id="restart-bot",
        symbol="SPY",
    )
    facts = ExecutionSliceFilledFacts(
        execution_id="restart-execution-1",
        symbol="SPY",
        side="BUY",
        slice_qty=2.0,
        slice_price=500.0,
        fee=0.0,
        fee_fidelity="reported",
        evidence_source="websocket",
        source_event_at_ms=1_786_368_000_101,
    )
    assert _append_slice(repo, accepted=accepted, facts=facts) == "appended"
    transitions_after_first_delivery = repo.custody_transitions()
    repo.close()

    reopened = ClerkSqliteRepository.open(
        account_id=_ACCOUNT_ID,
        artifacts_root=tmp_path,
        clock=_clock_at(1_786_368_000_000),
    )
    try:
        assert _append_slice(reopened, accepted=accepted, facts=facts) == "duplicate"
        assert reopened.custody_transitions() == transitions_after_first_delivery
        fills = reopened.fills_for_order(accepted.order_ref or "")
        assert len(fills) == 1
        assert fills[0]["fee"] == 0.0
        assert reopened.position("restart-bot", "SPY") == pytest.approx(2.0, abs=_ATOL, rel=0)
    finally:
        reopened.close()


def test_execution_correction_invalid_target_raises_uncertainty(tmp_path: Path) -> None:
    repo, accepted = _repository_for_strategy(
        tmp_path,
        strategy_instance_id="correction-guard-bot",
        symbol="SPY",
    )
    try:
        correction = ExecutionCorrectedFacts(
            execution_id="corrected-missing-target",
            superseded_execution_ref="missing-execution",
            symbol="SPY",
            side="BUY",
            corrected_qty=3.0,
            corrected_price=100.0,
            why="broker correction references an execution that is absent",
        )
        outcome = repo.append_execution_correction_or_raise(
            correction=_correction_transition(
                repo,
                accepted=accepted,
                facts=correction,
                source_event_at_ms=1_786_368_000_201,
            ),
            build_uncertainty=lambda reason: _correction_uncertainty_transition(
                repo,
                reason=reason,
                execution_id=correction.execution_id,
            ),
        )

        assert outcome == "invalid"
        assert repo.fills_for_order(accepted.order_ref or "") == []
        assert [transition["transition_kind"] for transition in repo.custody_transitions()][-1] == "UNCERTAINTY_RAISED"
        admission = decide_capability(
            repo,
            capability=Capability.NEW_EXPOSURE,
            strategy_instance_id="correction-guard-bot",
        )
        assert admission.allowed is False
        assert admission.reason_code == "EXECUTION_CORRECTION_UNEXPLAINED"
        transition_count = len(repo.custody_transitions())
        assert (
            repo.append_execution_correction_or_raise(
                correction=_correction_transition(
                    repo,
                    accepted=accepted,
                    facts=correction,
                    source_event_at_ms=1_786_368_000_201,
                ),
                build_uncertainty=lambda reason: _correction_uncertainty_transition(
                    repo,
                    reason=reason,
                    execution_id=correction.execution_id,
                ),
            )
            == "duplicate"
        )
        assert len(repo.custody_transitions()) == transition_count
    finally:
        repo.close()


def test_execution_correction_missing_target_symbol_raises_uncertainty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, accepted = _repository_for_strategy(
        tmp_path,
        strategy_instance_id="correction-symbol-bot",
        symbol="SPY",
    )
    try:
        monkeypatch.setattr(
            repository_module.reads,
            "effective_execution_slice",
            lambda _conn, _execution_id: {
                "order_ref": accepted.order_ref,
                "subject_id": f"bot:{accepted.command.strategy_instance_id}",
                "strategy_instance_id": accepted.command.strategy_instance_id,
                "symbol": None,
                "side": "BUY",
            },
        )
        correction = ExecutionCorrectedFacts(
            execution_id="corrected-missing-symbol",
            superseded_execution_ref="target-missing-symbol",
            symbol="SPY",
            side="BUY",
            corrected_qty=3.0,
            corrected_price=100.0,
            why="broker correction target lacks readable symbol evidence",
        )

        outcome = repo.append_execution_correction_or_raise(
            correction=_correction_transition(
                repo,
                accepted=accepted,
                facts=correction,
                source_event_at_ms=1_786_368_000_202,
            ),
            build_uncertainty=lambda reason: _correction_uncertainty_transition(
                repo,
                reason=reason,
                execution_id=correction.execution_id,
            ),
        )

        assert outcome == "invalid"
        uncertainty = UncertaintyRaisedFacts.from_facts_json(repo.custody_transitions()[-1]["facts_json"])
        assert uncertainty.reason_code == "EXECUTION_CORRECTION_UNEXPLAINED"
        assert uncertainty.explanation == "superseded execution is missing owned symbol evidence"
    finally:
        repo.close()


def test_cumulative_recovery_fill_is_explicitly_tagged(tmp_path: Path) -> None:
    repo, accepted = _repository_for_strategy(
        tmp_path,
        strategy_instance_id="recovery-bot",
        symbol="SPY",
    )
    try:
        exact = ExecutionSliceFilledFacts(
            execution_id="original-execution-5",
            symbol="SPY",
            side="BUY",
            slice_qty=5.0,
            slice_price=100.0,
            fee=None,
            fee_fidelity="not_reported",
            evidence_source="websocket",
            source_event_at_ms=1_786_368_000_301,
        )
        assert _append_slice(repo, accepted=accepted, facts=exact) == "appended"
        correction = ExecutionCorrectedFacts(
            execution_id="corrected-execution-3",
            superseded_execution_ref=exact.execution_id,
            symbol="SPY",
            side="BUY",
            corrected_qty=3.0,
            corrected_price=100.0,
            why="corrected broker quantity",
        )
        assert (
            repo.append_execution_correction_or_raise(
                correction=_correction_transition(
                    repo,
                    accepted=accepted,
                    facts=correction,
                    source_event_at_ms=1_786_368_000_302,
                ),
                build_uncertainty=lambda reason: _correction_uncertainty_transition(
                    repo,
                    reason=reason,
                    execution_id=correction.execution_id,
                ),
            )
            == "appended"
        )

        _append_cumulative_recovery(
            repo,
            accepted=accepted,
            cumulative_filled_quantity=4.0,
            average_price=100.0,
            source_event_at_ms=1_786_368_000_303,
        )

        fills = repo.fills_for_order(accepted.order_ref or "")
        recovery = fills[-1]
        assert recovery["execution_id"] is None
        assert recovery["evidence_source"] == "cumulative_recovery"
        assert recovery["qty"] == pytest.approx(1.0, abs=_ATOL, rel=0)
        assert repo.position("recovery-bot", "SPY") == pytest.approx(4.0, abs=_ATOL, rel=0)
    finally:
        repo.close()


def test_late_exact_execution_after_cumulative_recovery_auto_supersedes_coverage(
    tmp_path: Path,
) -> None:
    repo, accepted = _repository_for_strategy(
        tmp_path,
        strategy_instance_id="recovery-conflict-bot",
        symbol="SPY",
    )
    try:
        _append_cumulative_recovery(
            repo,
            accepted=accepted,
            cumulative_filled_quantity=5.0,
            average_price=100.0,
            source_event_at_ms=1_786_368_000_351,
        )
        late_exact = ExecutionSliceFilledFacts(
            execution_id="late-websocket-execution-5",
            symbol="SPY",
            side="BUY",
            slice_qty=5.0,
            slice_price=100.0,
            fee=0.15,
            fee_fidelity="reported",
            evidence_source="websocket",
            source_event_at_ms=1_786_368_000_352,
        )

        cumulative_transitions = repo.transitions_for_order(accepted.order_ref or "")
        assert _append_slice(repo, accepted=accepted, facts=late_exact) == "coverage_superseded"
        fills = repo.fills_for_order(accepted.order_ref or "")
        assert len(fills) == 1
        assert fills[0]["execution_id"] == late_exact.execution_id
        assert fills[0]["evidence_source"] == "websocket"
        assert fills[0]["qty"] == pytest.approx(5.0, abs=_ATOL, rel=0)
        assert fills[0]["price"] == pytest.approx(100.0, abs=_ATOL, rel=0)
        assert fills[0]["fee"] == pytest.approx(0.15, abs=_ATOL, rel=0)
        assert fills[0]["fee_fidelity"] == "reported"
        assert repo.position("recovery-conflict-bot", "SPY") == pytest.approx(
            5.0,
            abs=_ATOL,
            rel=0,
        )
        supersession_transitions = [
            transition
            for transition in repo.transitions_for_order(accepted.order_ref or "")
            if transition["transition_kind"] == "EXECUTION_COVERAGE_SUPERSEDED"
        ]
        assert len(supersession_transitions) == 1
        supersession = ExecutionCoverageSupersededFacts.from_facts_json(
            supersession_transitions[0]["facts_json"]
        )
        assert supersession.actor == "AUTOMATIC"
        assert supersession.order_ref == accepted.order_ref
        assert supersession.symbol == "SPY"
        assert supersession.side == "BUY"
        assert supersession.superseded_cumulative_fill_ids == [
            f"{accepted.order_ref}:5.000000000"
        ]
        assert supersession.exact_execution == late_exact
        assert supersession.exact_quantity == pytest.approx(5.0, abs=_ATOL, rel=0)
        assert supersession.exact_gross_cost == pytest.approx(500.0, abs=_ATOL, rel=0)
        assert supersession.cumulative_quantity == pytest.approx(5.0, abs=_ATOL, rel=0)
        assert supersession.cumulative_gross_cost == pytest.approx(500.0, abs=_ATOL, rel=0)
        assert supersession.resolved_uncertainty_id is None
        assert [item["row_hash"] for item in cumulative_transitions] == [
            item["row_hash"]
            for item in repo.transitions_for_order(accepted.order_ref or "")[: len(cumulative_transitions)]
        ]
        admission = decide_capability(
            repo,
            capability=Capability.NEW_EXPOSURE,
            strategy_instance_id="recovery-conflict-bot",
        )
        assert admission.allowed is True

        transitions_after_supersession = repo.transitions_for_order(accepted.order_ref or "")
        assert _append_slice(repo, accepted=accepted, facts=late_exact) == "duplicate"
        assert repo.transitions_for_order(accepted.order_ref or "") == transitions_after_supersession
    finally:
        repo.close()


def test_active_coverage_episode_prevents_direct_exact_supersession(tmp_path: Path) -> None:
    repo, accepted = _repository_for_strategy(
        tmp_path,
        strategy_instance_id="coverage-episode-bot",
        symbol="SPY",
    )
    try:
        _append_cumulative_recovery(
            repo,
            accepted=accepted,
            cumulative_filled_quantity=5.0,
            average_price=100.0,
            source_event_at_ms=1_786_368_000_351,
        )
        exact = ExecutionSliceFilledFacts(
            execution_id="coverage-active-episode-exact",
            symbol="SPY",
            side="BUY",
            slice_qty=5.0,
            slice_price=100.0,
            fee=None,
            fee_fidelity="not_reported",
            evidence_source="websocket",
            source_event_at_ms=1_786_368_000_352,
        )
        repo.append_transition(_coverage_conflict_transition(repo, accepted=accepted, facts=exact))

        assert _append_slice(repo, accepted=accepted, facts=exact) == "coverage_conflict_quarantined"
        assert not repo.has_order_transition(
            order_ref=accepted.order_ref or "",
            transition_kind="EXECUTION_COVERAGE_SUPERSEDED",
        )
        assert [fill["execution_id"] for fill in repo.fills_for_order(accepted.order_ref or "")] == [None]
        admission = decide_capability(
            repo,
            capability=Capability.NEW_EXPOSURE,
            strategy_instance_id="coverage-episode-bot",
        )
        assert admission.allowed is False
        assert admission.reason_code == EXECUTION_COVERAGE_CONFLICT_REASON_CODE
    finally:
        repo.close()


def test_stale_direct_coverage_proof_uses_the_existing_fail_closed_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, accepted = _repository_for_strategy(
        tmp_path,
        strategy_instance_id="coverage-stale-proof-bot",
        symbol="SPY",
    )
    try:
        _append_cumulative_recovery(
            repo,
            accepted=accepted,
            cumulative_filled_quantity=5.0,
            average_price=100.0,
            source_event_at_ms=1_786_368_000_351,
        )
        exact = ExecutionSliceFilledFacts(
            execution_id="coverage-stale-proof-exact",
            symbol="SPY",
            side="BUY",
            slice_qty=5.0,
            slice_price=100.0,
            fee=None,
            fee_fidelity="not_reported",
            evidence_source="websocket",
            source_event_at_ms=1_786_368_000_352,
        )
        original_proof = coverage_api_module.prove_execution_coverage_set
        changed_revision = False
        revision_before = repo.control_meta_snapshot().control_revision

        def proof_then_open_episode(candidate: object) -> object:
            nonlocal changed_revision
            result = original_proof(candidate)
            if not changed_revision:
                changed_revision = True
                repo.append_transition(_coverage_conflict_transition(repo, accepted=accepted, facts=exact))
            return result

        monkeypatch.setattr(coverage_api_module, "prove_execution_coverage_set", proof_then_open_episode)
        transitions_before = repo.transitions_for_order(accepted.order_ref or "")

        assert _append_slice(repo, accepted=accepted, facts=exact) == "coverage_conflict_quarantined"
        assert changed_revision is True
        assert repo.control_meta_snapshot().control_revision == revision_before + 2
        transitions_after = repo.transitions_for_order(accepted.order_ref or "")
        assert [item["transition_kind"] for item in transitions_after[len(transitions_before) :]] == [
            "UNCERTAINTY_RAISED",
            "EXECUTION_COVERAGE_QUARANTINED",
        ]
        assert not any(
            item["transition_kind"] == "EXECUTION_COVERAGE_SUPERSEDED"
            for item in transitions_after
        )
        assert [fill["execution_id"] for fill in repo.fills_for_order(accepted.order_ref or "")] == [None]
    finally:
        repo.close()


def test_direct_coverage_supersession_rebuilds_and_redelivers_once(tmp_path: Path) -> None:
    repo, accepted = _repository_for_strategy(
        tmp_path,
        strategy_instance_id="coverage-rebuild-bot",
        symbol="SPY",
    )
    repo_closed = False
    rebuilt: ClerkSqliteRepository | None = None
    try:
        _append_cumulative_recovery(
            repo,
            accepted=accepted,
            cumulative_filled_quantity=5.0,
            average_price=100.0,
            source_event_at_ms=1_786_368_000_351,
        )
        exact = ExecutionSliceFilledFacts(
            execution_id="coverage-rebuild-exact",
            symbol="SPY",
            side="BUY",
            slice_qty=5.0,
            slice_price=100.0,
            fee=None,
            fee_fidelity="not_reported",
            evidence_source="websocket",
            source_event_at_ms=1_786_368_000_352,
        )
        assert _append_slice(repo, accepted=accepted, facts=exact) == "coverage_superseded"
        original_transitions = repo.custody_transitions()
        original_fills = repo.fills_for_order(accepted.order_ref or "")
        db_path = repo.db_path
        repo.close()
        repo_closed = True
        db_path.rename(db_path.with_suffix(".db.corrupt"))

        rebuilt = ClerkSqliteRepository.rebuild_from_mirror(
            account_id=_ACCOUNT_ID,
            artifacts_root=tmp_path,
            clock=_clock_at(1_786_368_000_400),
        )
        assert rebuilt.custody_transitions() == original_transitions
        assert rebuilt.fills_for_order(accepted.order_ref or "") == original_fills
        assert rebuilt.position("coverage-rebuild-bot", "SPY") == pytest.approx(5.0, abs=_ATOL, rel=0)
        assert _append_slice(rebuilt, accepted=accepted, facts=exact) == "duplicate"
        assert rebuilt.custody_transitions() == original_transitions
    finally:
        if rebuilt is not None:
            rebuilt.close()
        if not repo_closed:
            repo.close()


def test_coverage_conflict_retains_each_distinct_exact_execution(tmp_path: Path) -> None:
    """A second exact slice stays auditable and cannot unlock a one-slice proof."""
    repo, accepted = _repository_for_strategy(
        tmp_path,
        strategy_instance_id="coverage-multiple-exacts",
        symbol="SPY",
    )
    try:
        _append_cumulative_recovery(
            repo,
            accepted=accepted,
            cumulative_filled_quantity=5.0,
            average_price=100.0,
            source_event_at_ms=1_786_368_000_351,
        )
        first_exact = ExecutionSliceFilledFacts(
            execution_id="coverage-multiple-a",
            symbol="SPY",
            side="BUY",
            slice_qty=2.0,
            slice_price=100.0,
            fee=None,
            fee_fidelity="not_reported",
            evidence_source="websocket",
            source_event_at_ms=1_786_368_000_352,
        )
        second_exact = ExecutionSliceFilledFacts(
            execution_id="coverage-multiple-b",
            symbol="SPY",
            side="BUY",
            slice_qty=3.0,
            slice_price=100.0,
            fee=None,
            fee_fidelity="not_reported",
            evidence_source="websocket",
            source_event_at_ms=1_786_368_000_353,
        )

        assert _append_slice(repo, accepted=accepted, facts=first_exact) == "coverage_conflict_raised"
        assert _append_slice(repo, accepted=accepted, facts=second_exact) == "coverage_conflict_quarantined"
        quarantines = [
            ExecutionCoverageQuarantinedFacts.from_facts_json(transition["facts_json"])
            for transition in repo.transitions_for_order(accepted.order_ref or "")
            if transition["transition_kind"] == "EXECUTION_COVERAGE_QUARANTINED"
        ]
        assert [item.exact_execution.execution_id for item in quarantines] == [
            first_exact.execution_id,
            second_exact.execution_id,
        ]
        assert quarantines[0].uncertainty is not None
        assert quarantines[1].uncertainty is None
        assert quarantines[1].conflict_execution_id == first_exact.execution_id
        active = repo.active_uncertainties_for_admission(
            strategy_instance_id="coverage-multiple-exacts"
        )
        assert len(active) == 1
        meta = repo.control_meta_snapshot()
        with pytest.raises(ExecutionCoverageResolutionUnavailable, match="Multiple exact executions"):
            repo.resolve_execution_coverage_conflict(
                uncertainty_id=active[0]["uncertainty_id"],
                expected_authority_generation=meta.authority_generation,
                expected_db_identity_token=meta.db_identity_token,
                expected_control_revision=meta.control_revision,
            )
        reader = SqliteClerkProjectionReader.from_repository(repo, clock=repo.clock)
        try:
            context = reader.recovery_context(strategy_instance_id="coverage-multiple-exacts")
        finally:
            reader.close()
        assert context is not None
        action = next(
            item
            for item in build_recovery_catalog(context)
            if item.action_id == "resolve_execution_coverage"
        )
        assert action.available is False
        assert [evidence.reference for evidence in action.evidence] == [
            f"order:{accepted.order_ref}",
            f"execution:{first_exact.execution_id}",
            f"execution:{second_exact.execution_id}",
            f"fill:{accepted.order_ref}:5.000000000",
        ]
        assert repo.active_uncertainties_for_admission(
            strategy_instance_id="coverage-multiple-exacts"
        ) == active
        assert [fill["execution_id"] for fill in repo.fills_for_order(accepted.order_ref or "")] == [None]
        assert _append_slice(repo, accepted=accepted, facts=second_exact) == "coverage_conflict_already_raised"
        assert len(
            [
                item
                for item in repo.transitions_for_order(accepted.order_ref or "")
                if item["transition_kind"] == "EXECUTION_COVERAGE_QUARANTINED"
            ]
        ) == 2
    finally:
        repo.close()


def test_coverage_resolution_replaces_one_cumulative_fill_once(tmp_path: Path) -> None:
    repo, accepted = _repository_for_strategy(
        tmp_path,
        strategy_instance_id="coverage-resolution-bot",
        symbol="SPY",
    )
    try:
        _append_cumulative_recovery(
            repo,
            accepted=accepted,
            cumulative_filled_quantity=5.0,
            average_price=100.0,
            source_event_at_ms=1_786_368_000_351,
        )
        exact = ExecutionSliceFilledFacts(
            execution_id="coverage-resolution-execution-5",
            symbol="SPY",
            side="BUY",
            slice_qty=5.0,
            slice_price=100.0,
            fee=None,
            fee_fidelity="not_reported",
            evidence_source="websocket",
            source_event_at_ms=1_786_368_000_352,
        )
        repo.append_transition(_coverage_conflict_transition(repo, accepted=accepted, facts=exact))
        assert _append_slice(repo, accepted=accepted, facts=exact) == "coverage_conflict_quarantined"
        meta = repo.control_meta_snapshot()
        active = repo.active_uncertainties_for_admission(
            strategy_instance_id="coverage-resolution-bot"
        )
        assert len(active) == 1
        receipt = repo.resolve_execution_coverage_conflict(
            uncertainty_id=active[0]["uncertainty_id"],
            expected_authority_generation=meta.authority_generation,
            expected_db_identity_token=meta.db_identity_token,
            expected_control_revision=meta.control_revision,
        )

        assert receipt.applied is True
        assert receipt.execution_id == exact.execution_id
        assert receipt.receipt_id.startswith("coverage-resolution:")
        resolution = next(
            transition
            for transition in repo.transitions_for_order(accepted.order_ref or "")
            if transition["transition_kind"] == "EXECUTION_COVERAGE_RESOLVED"
        )
        resolution_facts = ExecutionCoverageResolvedFacts.from_facts_json(
            resolution["facts_json"]
        )
        assert resolution_facts.account_id == _ACCOUNT_ID
        assert resolution_facts.authority_generation == meta.authority_generation
        assert resolution_facts.db_identity_token == meta.db_identity_token
        assert resolution_facts.expected_control_revision == meta.control_revision
        fills = repo.fills_for_order(accepted.order_ref or "")
        assert len(fills) == 1
        assert fills[0]["execution_id"] == exact.execution_id
        assert fills[0]["evidence_source"] == "websocket"
        assert repo.position("coverage-resolution-bot", "SPY") == pytest.approx(
            5.0,
            abs=_ATOL,
            rel=0,
        )
        assert repo.active_uncertainties_for_admission(
            strategy_instance_id="coverage-resolution-bot"
        ) == []
        assert decide_capability(
            repo,
            capability=Capability.REDUCE,
            strategy_instance_id="coverage-resolution-bot",
        ).allowed

        retry = repo.resolve_execution_coverage_conflict(
            uncertainty_id=active[0]["uncertainty_id"],
            expected_authority_generation=meta.authority_generation,
            expected_db_identity_token=meta.db_identity_token,
            expected_control_revision=meta.control_revision,
        )
        assert retry == receipt.__class__(**{**receipt.__dict__, "applied": False})
        assert repo.fills_for_order(accepted.order_ref or "") == fills
    finally:
        repo.close()


def test_coverage_resolution_survives_restart_without_a_second_delta(tmp_path: Path) -> None:
    repo, accepted = _repository_for_strategy(
        tmp_path,
        strategy_instance_id="coverage-restart",
        symbol="SPY",
    )
    try:
        _append_cumulative_recovery(
            repo,
            accepted=accepted,
            cumulative_filled_quantity=5.0,
            average_price=100.0,
            source_event_at_ms=1_786_368_000_351,
        )
        exact = ExecutionSliceFilledFacts(
            execution_id="coverage-restart-execution-5",
            symbol="SPY",
            side="BUY",
            slice_qty=5.0,
            slice_price=100.0,
            fee=None,
            fee_fidelity="not_reported",
            evidence_source="websocket",
            source_event_at_ms=1_786_368_000_352,
        )
        repo.append_transition(_coverage_conflict_transition(repo, accepted=accepted, facts=exact))
        assert _append_slice(repo, accepted=accepted, facts=exact) == "coverage_conflict_quarantined"
        uncertainty_id = repo.active_uncertainties_for_admission(
            strategy_instance_id="coverage-restart"
        )[0]["uncertainty_id"]
        repo.close()
        repo = ClerkSqliteRepository.open(
            account_id=_ACCOUNT_ID,
            artifacts_root=tmp_path,
            clock=_clock_at(1_786_368_000_400),
        )
        meta = repo.control_meta_snapshot()
        receipt = repo.resolve_execution_coverage_conflict(
            uncertainty_id=uncertainty_id,
            expected_authority_generation=meta.authority_generation,
            expected_db_identity_token=meta.db_identity_token,
            expected_control_revision=meta.control_revision,
        )

        assert receipt.applied is True
        assert repo.position("coverage-restart", "SPY") == pytest.approx(
            5.0,
            abs=_ATOL,
            rel=0,
        )
        assert repo.fills_for_order(accepted.order_ref or "")[0]["execution_id"] == exact.execution_id
    finally:
        repo.close()


@pytest.mark.asyncio
async def test_presented_coverage_resolution_replays_its_original_receipt(
    tmp_path: Path,
) -> None:
    repo, accepted = _repository_for_strategy(
        tmp_path,
        strategy_instance_id="presented-cover",
        symbol="SPY",
    )
    try:
        _append_cumulative_recovery(
            repo,
            accepted=accepted,
            cumulative_filled_quantity=5.0,
            average_price=100.0,
            source_event_at_ms=1_786_368_000_351,
        )
        exact = ExecutionSliceFilledFacts(
            execution_id="presented-coverage-resolution-execution-5",
            symbol="SPY",
            side="BUY",
            slice_qty=5.0,
            slice_price=100.0,
            fee=None,
            fee_fidelity="not_reported",
            evidence_source="websocket",
            source_event_at_ms=1_786_368_000_352,
        )
        repo.append_transition(_coverage_conflict_transition(repo, accepted=accepted, facts=exact))
        assert _append_slice(repo, accepted=accepted, facts=exact) == "coverage_conflict_quarantined"

        async def current_context() -> RecoveryPolicyContext:
            reader = SqliteClerkProjectionReader.from_repository(repo, clock=repo.clock)
            try:
                context = reader.recovery_context(
                    strategy_instance_id="presented-cover"
                )
                assert context is not None
                return context
            finally:
                reader.close()

        context = await current_context()
        action = next(
            item
            for item in build_recovery_catalog(context)
            if item.action_id == "resolve_execution_coverage"
        )
        assert action.available is True
        assert action.execution_ref is not None

        class _Facade:
            account_id = _ACCOUNT_ID
            repository = repo

        request = RecoveryExecutionRequest(
            action_id="resolve_execution_coverage",
            concurrency_token=action.concurrency_token,
            execution_ref=action.execution_ref,
            reason=None,
        )
        result = await execute_recovery_action(
            _Facade(),
            request=request,
            current_context=current_context,
        )
        replay = await execute_recovery_action(
            _Facade(),
            request=request,
            current_context=current_context,
        )

        assert result.applied is True
        assert replay.applied is False
        assert replay.receipt_id == result.receipt_id
    finally:
        repo.close()


def test_execution_slice_filled_rejects_invalid_facts(tmp_path: Path) -> None:
    repo, accepted = _repository_for_strategy(
        tmp_path,
        strategy_instance_id="invalid-facts-bot",
        symbol="SPY",
    )
    try:
        invalid = ExecutionSliceFilledFacts(
            execution_id="invalid-fee",
            symbol="SPY",
            side="BUY",
            slice_qty=1.0,
            slice_price=100.0,
            fee=0.0,
            fee_fidelity="not_reported",
            evidence_source="websocket",
            source_event_at_ms=1_786_368_000_401,
        )
        with pytest.raises(ValueError, match="fee=None"):
            _append_slice(repo, accepted=accepted, facts=invalid)
        assert repo.fills_for_order(accepted.order_ref or "") == []
    finally:
        repo.close()
