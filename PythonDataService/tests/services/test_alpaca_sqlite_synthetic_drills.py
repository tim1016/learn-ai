from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.account_custody_synthetic_scenarios import (
    SYNTHETIC_CUSTODY_SCENARIO_IDS,
    SyntheticScenarioId,
    synthetic_scenario,
)
from app.services.alpaca_sqlite_synthetic_drills import (
    FaultSeamLimitationCode,
    SyntheticScenarioStatus,
    run_synthetic_custody_drills,
)
from app.services.alpaca_sqlite_synthetic_fault_drills import lost_cancel as run_lost_cancel
from app.services.alpaca_sqlite_synthetic_frame_drills import cancel_fill_race, gap_reconcile
from app.services.alpaca_sqlite_synthetic_state_drills import restart_in_flight


@pytest.fixture(autouse=True)
def permit_paper_fault_seam(monkeypatch: pytest.MonkeyPatch):
    from app.broker.alpaca import fault_injection as fi

    monkeypatch.setattr(fi.settings, "ALPACA_FAULT_INJECTION_ENABLED", True)
    monkeypatch.setattr(fi, "get_alpaca_settings", lambda: SimpleNamespace(is_paper=True))
    fi.reset_fault_injection_for_testing()
    yield fi
    fi.reset_fault_injection_for_testing()


async def test_run_synthetic_custody_drills_returns_observed_canonical_matrix(
    tmp_path: Path,
) -> None:
    report = await run_synthetic_custody_drills(tmp_path)

    assert tuple(item.scenario_id for item in report.observations) == (
        SYNTHETIC_CUSTODY_SCENARIO_IDS
    )
    assert report.live_scenario_executed is False
    assert report.production_generation_opened_read_write is False
    assert report.fault_injection_permitted is True
    assert report.fault_seam_exercised is True
    assert report.fault_registry_write_armed == ()
    assert report.fault_registry_frame_armed == ()
    assert all(item.status is not SyntheticScenarioStatus.FAILED for item in report.observations)

    for observation in report.observations:
        evidence = observation.evidence
        assert evidence.authority_generation == 1
        assert evidence.db_identity_token
        assert evidence.revision_after > evidence.revision_before
        assert evidence.source_event_at_ms is not None
        assert evidence.clerk_observed_at_ms > 0
        assert evidence.recorded_at_ms > 0
        assert evidence.source_event_at_ms <= evidence.clerk_observed_at_ms <= evidence.recorded_at_ms
        assert evidence.durable_transition_ref.startswith("transition:")
        assert len(evidence.log_refs) == 2
        assert evidence.broker_after.lookup_calls is not None
        assert synthetic_scenario(observation.scenario_id).test_refs


async def test_source_accurate_faults_cross_production_hooks(tmp_path: Path) -> None:
    report = await run_synthetic_custody_drills(tmp_path)
    by_id = {item.scenario_id: item for item in report.observations}

    lost_submit = by_id[SyntheticScenarioId.LOST_SUBMIT_EXACT_IDENTITY]
    lost_cancel = by_id[SyntheticScenarioId.LOST_CANCEL_RETAINS_CUSTODY]
    duplicate = by_id[SyntheticScenarioId.DUPLICATE_TRADE_UPDATE]

    assert lost_submit.status is SyntheticScenarioStatus.PASSED
    assert lost_cancel.status is SyntheticScenarioStatus.PASSED
    assert duplicate.status is SyntheticScenarioStatus.PASSED
    assert lost_submit.limitations == ()
    assert lost_cancel.limitations == ()
    assert duplicate.limitations == ()
    assert lost_submit.fault_seam_exercised is True
    assert lost_cancel.fault_seam_exercised is True
    assert duplicate.fault_seam_exercised is True


async def test_synthetic_custody_drills_capture_real_identity_and_broker_proofs(
    tmp_path: Path,
) -> None:
    report = await run_synthetic_custody_drills(tmp_path)
    by_id = {item.scenario_id: item for item in report.observations}

    exact = by_id[SyntheticScenarioId.LOST_SUBMIT_EXACT_IDENTITY].evidence
    assert exact.order_ref is not None
    assert exact.broker_order_id is not None
    assert exact.broker_after.lookup_calls == (exact.order_ref,)

    cancel_fill_observation = by_id[SyntheticScenarioId.CANCEL_FILL_RACE]
    assert cancel_fill_observation.status is SyntheticScenarioStatus.PASSED
    cancel_fill = cancel_fill_observation.evidence
    assert cancel_fill.effect_operation_id is not None
    assert cancel_fill.broker_after.cancel_calls
    assert len(cancel_fill.order_refs) == 2
    assert set(cancel_fill.broker_order_ids) == {order.broker_order_id for order in cancel_fill.broker_after.orders}
    assert any(order.filled_quantity == 4.0 for order in cancel_fill.broker_after.orders)
    assert any(order.side == "sell" and order.quantity == 4.0 for order in cancel_fill.broker_after.orders)

    duplicate = by_id[SyntheticScenarioId.DUPLICATE_TRADE_UPDATE].evidence
    duplicate_broker_ids = {order.broker_order_id for order in duplicate.broker_after.orders}
    assert len(duplicate.broker_order_ids) == 1
    assert set(duplicate.broker_order_ids) == duplicate_broker_ids
    duplicate_broker_order_id = duplicate.broker_order_ids[0]
    assert (
        f"frame_broker_order_ids=('{duplicate_broker_order_id}', '{duplicate_broker_order_id}')"
    ) in duplicate.observed
    assert f"clerk_broker_order_id={duplicate_broker_order_id}" in duplicate.observed
    assert f"broker_order_id={duplicate_broker_order_id}" in duplicate.observed
    assert "identity_stable=True" in duplicate.observed

    lost_cancel = by_id[SyntheticScenarioId.LOST_CANCEL_RETAINS_CUSTODY].evidence
    assert {order.broker_order_id for order in lost_cancel.broker_after.orders}.issubset(
        set(lost_cancel.broker_order_ids)
    )

    gap_observation = by_id[SyntheticScenarioId.TRADE_UPDATE_GAP_RECONCILIATION]
    assert gap_observation.status is SyntheticScenarioStatus.PASSED
    assert gap_observation.fault_seam_exercised is True
    gap = gap_observation.evidence
    assert "before=False" in gap.observed
    assert "during=False" in gap.observed
    assert "during_reason=RECONCILIATION_IN_PROGRESS" in gap.observed
    assert "after=True" in gap.observed
    assert {order.broker_order_id for order in gap.broker_after.orders}.issubset(set(gap.broker_order_ids))

    restart = by_id[SyntheticScenarioId.RESTART_IN_FLIGHT_WORK].evidence
    assert len(restart.order_refs) == 2
    assert restart.broker_after.submit_calls == restart.broker_before.submit_calls
    assert restart.broker_after.lookup_calls[len(restart.broker_before.lookup_calls) :] == restart.order_refs


async def test_cancel_fill_fault_is_folded_while_cancel_is_in_flight(
    tmp_path: Path,
) -> None:
    observation = await cancel_fill_race(tmp_path)

    observed = dict(field.split("=", maxsplit=1) for field in observation.evidence.observed.split("; "))
    cancel_requested = int(observed["cancel_requested_sequence"])
    partial_fill = int(observed["partial_fill_sequence"])
    terminal = int(observed["terminal_sequence"])

    assert observation.status is SyntheticScenarioStatus.PASSED
    assert observation.fault_seam_exercised is True
    assert observed["race_ordered"] == "True"
    assert observed["identity_stable"] == "True"
    entry_broker_order_id = next(
        order.broker_order_id for order in observation.evidence.broker_after.orders if order.side == "buy"
    )
    assert observed["frame_broker_order_ids"] == f"('{entry_broker_order_id}',)"
    assert observed["clerk_broker_order_id"] == entry_broker_order_id
    assert observed["broker_order_id"] == entry_broker_order_id
    assert cancel_requested < partial_fill < terminal
    assert "cancel requested < partial fill observed < terminal proven" in (observation.evidence.expected)
    assert len(observation.evidence.broker_after.cancel_calls) == 1


async def test_lost_cancel_sdk_call_is_in_structured_broker_proof(
    tmp_path: Path,
) -> None:
    observation = await run_lost_cancel(tmp_path)
    before = observation.evidence.broker_before
    after = observation.evidence.broker_after

    assert observation.status is SyntheticScenarioStatus.PASSED
    assert observation.fault_seam_exercised is True
    assert before.cancel_calls == ()
    assert len(after.cancel_calls) == 1
    assert after.cancel_calls[0] in {order.broker_order_id for order in before.orders}
    assert f"structured_cancel_calls={after.cancel_calls}" in (observation.evidence.observed)


async def test_gap_reconciliation_blocks_admission_while_rest_snapshot_is_in_flight(
    tmp_path: Path,
) -> None:
    observation = await gap_reconcile(tmp_path)

    assert observation.status is SyntheticScenarioStatus.PASSED
    assert observation.fault_seam_exercised is True
    assert "before=False" in observation.evidence.observed
    assert "during=False" in observation.evidence.observed
    assert "during_reason=RECONCILIATION_IN_PROGRESS" in observation.evidence.observed
    assert "after=True" in observation.evidence.observed


async def test_restart_in_flight_uses_active_authority_boot_recovery_for_accepted_and_unknown_work(
    tmp_path: Path,
) -> None:
    observation = await restart_in_flight(tmp_path)
    before = observation.evidence.broker_before
    after = observation.evidence.broker_after

    assert observation.status is SyntheticScenarioStatus.PASSED
    assert len(observation.evidence.order_refs) == 2
    assert after.submit_calls == before.submit_calls
    assert after.lookup_calls[len(before.lookup_calls) :] == observation.evidence.order_refs
    assert "accepted_state=in_progress" in observation.evidence.observed
    assert "unknown_state=in_progress" in observation.evidence.observed
    assert "unknown_hold=False" in observation.evidence.observed
    assert "boot_submit_count=0" in observation.evidence.observed


async def test_disabled_fault_seam_stays_typed_partial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.broker.alpaca import fault_injection as fi

    monkeypatch.setattr(fi.settings, "ALPACA_FAULT_INJECTION_ENABLED", False)
    fi.reset_fault_injection_for_testing()

    report = await run_synthetic_custody_drills(tmp_path)
    by_id = {item.scenario_id: item for item in report.observations}

    assert report.fault_injection_permitted is False
    assert report.fault_seam_exercised is False
    for scenario_id in (
        SyntheticScenarioId.LOST_SUBMIT_EXACT_IDENTITY,
        SyntheticScenarioId.DUPLICATE_TRADE_UPDATE,
        SyntheticScenarioId.CANCEL_FILL_RACE,
        SyntheticScenarioId.LOST_CANCEL_RETAINS_CUSTODY,
        SyntheticScenarioId.TRADE_UPDATE_GAP_RECONCILIATION,
    ):
        observation = by_id[scenario_id]
        assert observation.status is SyntheticScenarioStatus.PARTIAL
        assert observation.fault_seam_exercised is False
        assert any(item.code is FaultSeamLimitationCode.FAULT_SEAM_NOT_PERMITTED for item in observation.limitations)
