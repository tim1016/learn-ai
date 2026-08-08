"""Canonical typed scenario registry for the #1415 synthetic rehearsal."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from app.models.account_custody_synthetic import (
    SyntheticAbortCause,
    SyntheticScenarioCategory,
    SyntheticScenarioId,
)
from app.models.account_custody_synthetic import (
    synthetic_abort_cause_for_scenario as _synthetic_abort_cause_for_scenario,
)


@dataclass(frozen=True)
class SyntheticRehearsalScenario:
    """One rehearsal row and every public regression that supports it."""

    scenario_id: SyntheticScenarioId
    name: str
    category: SyntheticScenarioCategory
    abort_cause: SyntheticAbortCause
    expected_state: str
    test_refs: tuple[str, ...]


SYNTHETIC_REHEARSAL_SCENARIOS: tuple[SyntheticRehearsalScenario, ...] = (
    SyntheticRehearsalScenario(
        SyntheticScenarioId.POLYGON_REPLAY_CLOSED_MINUTES,
        "Polygon minute aggregates traverse the live 5-second aggregation path",
        SyntheticScenarioCategory.FEED,
        _synthetic_abort_cause_for_scenario(SyntheticScenarioId.POLYGON_REPLAY_CLOSED_MINUTES),
        "Exact fixture OHLCV emerges as closed RTH minutes with int64 ms UTC clocks and each minute reaches the canonical decision kernel.",
        (
            "tests/broker/alpaca/clerk/sqlite/test_qualification.py::test_polygon_fixture_replays_through_live_feed_path",
        ),
    ),
    SyntheticRehearsalScenario(
        SyntheticScenarioId.FEED_ONE_PRINT_STALL,
        "One source print then silence fails closed and replaces the line",
        SyntheticScenarioCategory.FEED,
        _synthetic_abort_cause_for_scenario(SyntheticScenarioId.FEED_ONE_PRINT_STALL),
        "Source health is stale by 30 seconds and the 60-second line invalidation is recoverable.",
        (
            "tests/services/test_bot_start_admission.py::test_one_print_stall_blocks_admission_then_replaces_subscription",
            "tests/broker/ibkr/test_bars.py::test_minute_stream_one_print_then_silence_invalidates_without_a_closed_bar",
            "tests/broker/ibkr/test_bars.py::test_raw_stream_invalidates_a_bounded_stalled_subscription",
            "tests/marketdata/test_feed.py::test_stalled_subscription_is_replaced_without_ending_the_bot_feed",
        ),
    ),
    SyntheticRehearsalScenario(
        SyntheticScenarioId.FEED_NEVER_FIRST_BAR,
        "Never-first-bar feed blocks admission",
        SyntheticScenarioCategory.FEED,
        _synthetic_abort_cause_for_scenario(SyntheticScenarioId.FEED_NEVER_FIRST_BAR),
        "An active feed with no closed minute is stale immediately and cannot authorize exposure.",
        (
            "tests/marketdata/test_feed.py::test_health_active_without_first_closed_bar_is_stale",
            "tests/services/test_bot_start_admission.py::test_never_advanced_feed_fails_closed",
        ),
    ),
    SyntheticRehearsalScenario(
        SyntheticScenarioId.LOST_SUBMIT_EXACT_IDENTITY,
        "Lost submit resolves by exact client identity",
        SyntheticScenarioCategory.CUSTODY,
        _synthetic_abort_cause_for_scenario(SyntheticScenarioId.LOST_SUBMIT_EXACT_IDENTITY),
        "Timeout opens uncertainty; exact lookup resolves one order and no duplicate intent is admitted.",
        (
            "tests/broker/alpaca/test_client.py::test_injected_post_sdk_submit_hides_landed_response",
            "tests/broker/alpaca/clerk/sqlite/test_enter.py::test_lost_submit_atomically_blocks_more_exposure_until_exact_recovery",
        ),
    ),
    SyntheticRehearsalScenario(
        SyntheticScenarioId.DUPLICATE_TRADE_UPDATE,
        "Duplicate/redelivered broker evidence is idempotent",
        SyntheticScenarioCategory.CUSTODY,
        _synthetic_abort_cause_for_scenario(SyntheticScenarioId.DUPLICATE_TRADE_UPDATE),
        "The paper-only seam redelivers the real frame and the Clerk counts the fill once.",
        (
            "tests/broker/alpaca/test_trade_updates.py::test_inject_frame_faults_redelivers_last_trade_update",
            "tests/broker/alpaca/clerk/sqlite/test_enter.py::test_duplicate_fill_observation_does_not_double_count",
        ),
    ),
    SyntheticRehearsalScenario(
        SyntheticScenarioId.CANCEL_FILL_RACE,
        "Cancel-first EXIT retains proven remainder during a fill race",
        SyntheticScenarioCategory.CUSTODY,
        _synthetic_abort_cause_for_scenario(SyntheticScenarioId.CANCEL_FILL_RACE),
        "The injected frame follows the real consumer and only Clerk-proven remaining quantity closes.",
        (
            "tests/broker/alpaca/test_trade_updates.py::test_injected_frame_threads_through_the_real_consumer",
            "tests/broker/alpaca/clerk/sqlite/test_exit.py::test_partial_fill_during_cancel_uses_only_the_clerk_proven_remaining_quantity",
            "tests/services/test_alpaca_sqlite_synthetic_drills.py::test_cancel_fill_fault_is_folded_while_cancel_is_in_flight",
        ),
    ),
    SyntheticRehearsalScenario(
        SyntheticScenarioId.LOST_CANCEL_RETAINS_CUSTODY,
        "Lost cancel response retains custody",
        SyntheticScenarioCategory.CUSTODY,
        _synthetic_abort_cause_for_scenario(SyntheticScenarioId.LOST_CANCEL_RETAINS_CUSTODY),
        "The seam short-circuits cancel and the Clerk blocks a closing order until terminal proof exists.",
        (
            "tests/broker/alpaca/test_client.py::test_injected_post_sdk_cancel_hides_landed_response",
            "tests/broker/alpaca/clerk/sqlite/test_exit.py::test_lost_cancel_response_blocks_the_closing_order",
            "tests/services/test_alpaca_sqlite_synthetic_drills.py::test_lost_cancel_sdk_call_is_in_structured_broker_proof",
        ),
    ),
    SyntheticRehearsalScenario(
        SyntheticScenarioId.TRADE_UPDATE_GAP_RECONCILIATION,
        "Trade-update gap reconciles before admission",
        SyntheticScenarioCategory.CUSTODY,
        _synthetic_abort_cause_for_scenario(SyntheticScenarioId.TRADE_UPDATE_GAP_RECONCILIATION),
        "REST reconciliation completes and admission remains closed until the gap is resolved.",
        (
            "tests/broker/alpaca/test_trade_updates.py::test_injected_disconnect_enters_reconnect_path",
            "tests/broker/alpaca/test_trade_updates.py::test_reconnect_gap_reconcile_pulls_missed_orders",
            "tests/broker/alpaca/clerk/sqlite/test_reconcile.py::test_reconciliation_fences_enter_before_reading_broker_truth",
            "tests/services/test_alpaca_sqlite_synthetic_drills.py::test_gap_reconciliation_blocks_admission_while_rest_snapshot_is_in_flight",
        ),
    ),
    SyntheticRehearsalScenario(
        SyntheticScenarioId.BOT_UNCERTAINTY_ISOLATION,
        "BOT uncertainty isolates only the affected bot",
        SyntheticScenarioCategory.CUSTODY,
        _synthetic_abort_cause_for_scenario(SyntheticScenarioId.BOT_UNCERTAINTY_ISOLATION),
        "Sibling bots retain admission while the uncertain bot remains blocked.",
        (
            "tests/broker/alpaca/clerk/sqlite/test_uncertainty.py::test_admit_new_exposure_bot_scoped_uncertainty_blocks_only_that_bot",
        ),
    ),
    SyntheticRehearsalScenario(
        SyntheticScenarioId.UNCLASSIFIED_UNCERTAINTY_ACCOUNT_CLERK,
        "Unknown uncertainty defaults to ACCOUNT_CLERK",
        SyntheticScenarioCategory.CUSTODY,
        _synthetic_abort_cause_for_scenario(SyntheticScenarioId.UNCLASSIFIED_UNCERTAINTY_ACCOUNT_CLERK),
        "Unclassified uncertainty blocks new exposure account-wide without fabricating terminal state.",
        (
            "tests/broker/alpaca/clerk/sqlite/test_uncertainty.py::test_raise_uncertainty_default_shape_fails_closed",
            "tests/broker/alpaca/clerk/sqlite/test_uncertainty.py::test_unknown_cause_blocks_reduction_account_wide",
        ),
    ),
    SyntheticRehearsalScenario(
        SyntheticScenarioId.RESTART_IN_FLIGHT_WORK,
        "Restart resolves accepted/unknown work without duplication",
        SyntheticScenarioCategory.CUSTODY,
        _synthetic_abort_cause_for_scenario(SyntheticScenarioId.RESTART_IN_FLIGHT_WORK),
        "Durable state survives restart and recovery finds exactly one accepted operation.",
        (
            "tests/broker/alpaca/clerk/sqlite/test_commands.py::test_state_machine_survives_restart",
            "tests/broker/alpaca/clerk/sqlite/test_enter.py::test_kill_before_broker_contact_recovery_finds_one_accepted_operation_no_duplicate",
            "tests/broker/alpaca/clerk/test_active_authority.py::test_valid_activation_opens_and_recovers_only_sqlite",
            "tests/services/test_alpaca_sqlite_synthetic_drills.py::test_restart_in_flight_uses_active_authority_boot_recovery_for_accepted_and_unknown_work",
        ),
    ),
    SyntheticRehearsalScenario(
        SyntheticScenarioId.EVIDENCE_CONTROLS_READ_ONLY,
        "Evidence controls never dispatch a lifecycle command",
        SyntheticScenarioCategory.UI,
        _synthetic_abort_cause_for_scenario(SyntheticScenarioId.EVIDENCE_CONTROLS_READ_ONLY),
        "Repeated evidence interactions across actual browser reloads and native EventSource revisions issue evidence reads only.",
        (
            "Frontend/tests/e2e/alpaca-clerk-ui-correlation.spec.ts::keeps evidence clicks read-only across real browser reloads and native SSE revisions",
        ),
    ),
    SyntheticRehearsalScenario(
        SyntheticScenarioId.RECOVERY_CEREMONY,
        "Protected-generation backup, restore, and mirror rebuild",
        SyntheticScenarioCategory.RECOVERY,
        _synthetic_abort_cause_for_scenario(SyntheticScenarioId.RECOVERY_CEREMONY),
        "Backup, restored DB, and rebuilt DB pass integrity and share the exact hash head.",
        ("synthetic-runner:protected-generation-recovery-ceremony",),
    ),
)

_SCENARIOS_BY_ID = {scenario.scenario_id: scenario for scenario in SYNTHETIC_REHEARSAL_SCENARIOS}
if len(_SCENARIOS_BY_ID) != len(SYNTHETIC_REHEARSAL_SCENARIOS):
    raise RuntimeError("synthetic rehearsal scenario IDs must be unique")
if set(_SCENARIOS_BY_ID) != set(SyntheticScenarioId):
    raise RuntimeError("synthetic rehearsal registry must cover every scenario ID")

SYNTHETIC_REHEARSAL_SCENARIOS_BY_ID: Mapping[SyntheticScenarioId, SyntheticRehearsalScenario] = MappingProxyType(
    _SCENARIOS_BY_ID
)
SYNTHETIC_REHEARSAL_SCENARIO_IDS = tuple(scenario.scenario_id for scenario in SYNTHETIC_REHEARSAL_SCENARIOS)
SYNTHETIC_CUSTODY_SCENARIO_IDS = tuple(
    scenario.scenario_id
    for scenario in SYNTHETIC_REHEARSAL_SCENARIOS
    if scenario.category is SyntheticScenarioCategory.CUSTODY
)


def synthetic_scenario(
    scenario_id: SyntheticScenarioId,
) -> SyntheticRehearsalScenario:
    """Return the one canonical descriptor for ``scenario_id``."""

    return SYNTHETIC_REHEARSAL_SCENARIOS_BY_ID[scenario_id]


def synthetic_abort_cause_for_scenario(
    scenario_id: SyntheticScenarioId,
) -> SyntheticAbortCause:
    """Compatibility re-export for the neutral abort-policy helper."""

    return _synthetic_abort_cause_for_scenario(scenario_id)
