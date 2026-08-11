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
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.enter import EnterSubmission, accept_enter
from app.broker.alpaca.clerk.sqlite.facts import (
    ExecutionCorrectedFacts,
    ExecutionSliceFilledFacts,
    UncertaintyRaisedFacts,
)
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import Capability, decide_capability
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
    ExecutionCoverageConflictCause,
)
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg
from conftest import _clock_at

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


def _append_slice(
    repo: ClerkSqliteRepository,
    *,
    accepted: EnterSubmission,
    facts: ExecutionSliceFilledFacts,
) -> str:
    def _coverage_conflict_transition() -> TransitionInput:
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

    return repo.append_execution_slice_if_absent(
        execution_id=facts.execution_id,
        order_ref=accepted.order_ref or "",
        build_transition=lambda: _slice_transition(repo, accepted=accepted, facts=facts),
        build_coverage_conflict=_coverage_conflict_transition,
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


def test_late_exact_execution_after_cumulative_recovery_raises_one_coverage_conflict(
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
            fee=None,
            fee_fidelity="not_reported",
            evidence_source="websocket",
            source_event_at_ms=1_786_368_000_352,
        )

        assert _append_slice(repo, accepted=accepted, facts=late_exact) == "coverage_conflict_raised"
        fills = repo.fills_for_order(accepted.order_ref or "")
        assert len(fills) == 1
        assert fills[0]["evidence_source"] == "cumulative_recovery"
        assert repo.position("recovery-conflict-bot", "SPY") == pytest.approx(
            5.0,
            abs=_ATOL,
            rel=0,
        )
        uncertainty_transitions = [
            transition
            for transition in repo.transitions_for_order(accepted.order_ref or "")
            if transition["transition_kind"] == "UNCERTAINTY_RAISED"
        ]
        assert len(uncertainty_transitions) == 1
        uncertainty_facts = UncertaintyRaisedFacts.from_facts_json(
            uncertainty_transitions[0]["facts_json"]
        )
        assert uncertainty_facts.reason_code == EXECUTION_COVERAGE_CONFLICT_REASON_CODE
        assert uncertainty_facts.cause_facts == {
            "execution_id": late_exact.execution_id,
            "order_ref": accepted.order_ref,
        }
        admission = decide_capability(
            repo,
            capability=Capability.NEW_EXPOSURE,
            strategy_instance_id="recovery-conflict-bot",
        )
        assert admission.allowed is False
        assert admission.reason_code == EXECUTION_COVERAGE_CONFLICT_REASON_CODE

        transitions_after_conflict = repo.transitions_for_order(accepted.order_ref or "")
        assert (
            _append_slice(repo, accepted=accepted, facts=late_exact)
            == "coverage_conflict_already_raised"
        )
        assert repo.transitions_for_order(accepted.order_ref or "") == transitions_after_conflict
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
