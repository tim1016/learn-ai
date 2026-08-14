"""Canonical deterministic coverage matrix for manual SQLite paper release."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ManualQualificationScenario:
    """One release claim and the focused regressions that prove it pre-live."""

    scenario_id: str
    title: str
    test_refs: tuple[str, ...]


MANUAL_QUALIFICATION_SCENARIOS: tuple[ManualQualificationScenario, ...] = (
    ManualQualificationScenario(
        "offline_v8_to_v9_ceremony",
        "The v8 authority upgrades, resumes a post-publication interruption, and refuses a live lease.",
        (
            "tests/broker/alpaca/clerk/sqlite/test_offline_v9_upgrade.py::"
            "test_offline_upgrade_replays_a_production_shaped_v8_authority_and_is_idempotent",
            "tests/broker/alpaca/clerk/sqlite/test_offline_v9_upgrade.py::"
            "test_post_swap_interruption_is_finalized_from_the_durable_prepare_receipt",
            "tests/broker/alpaca/clerk/sqlite/test_offline_v9_upgrade.py::"
            "test_offline_upgrade_refuses_an_active_execution_lease_without_mutating_v8",
        ),
    ),
    ManualQualificationScenario(
        "market_buy_fill_and_projection",
        "A manual one-share buy reaches exact fill, subject position, and account transaction history.",
        (
            "tests/broker/alpaca/clerk/sqlite/test_manual_orders.py::"
            "test_manual_order_exact_coverage_tolerance_completes_the_ticket",
            "tests/broker/alpaca/clerk/test_trade_evidence.py::"
            "test_sqlite_websocket_fill_completes_manual_ticket_with_exact_coverage",
            "tests/services/test_sqlite_clerk_transaction_projection.py::"
            "test_account_transaction_projection_includes_manual_ticket_without_bot_run",
        ),
    ),
    ManualQualificationScenario(
        "manual_owned_reduction",
        "A manual sell reserves only its own long position and cannot borrow bot attribution.",
        (
            "tests/broker/alpaca/clerk/sqlite/test_manual_orders.py::"
            "test_manual_sell_reserves_only_its_subject_long_position",
            "tests/broker/alpaca/clerk/sqlite/test_manual_orders.py::"
            "test_manual_sell_reserves_only_its_unfilled_remainder",
        ),
    ),
    ManualQualificationScenario(
        "limit_and_cancel",
        "A resting limit/GTC ticket and its exact owned cancellation remain one durable resource.",
        (
            "tests/broker/alpaca/clerk/sqlite/test_manual_orders.py::"
            "test_manual_limit_gtc_leg_is_durable_and_submitted_once",
            "tests/broker/alpaca/clerk/sqlite/test_manual_orders.py::"
            "test_manual_cancel_is_durable_idempotent_and_proves_exact_target",
            "tests/broker/alpaca/clerk/sqlite/test_manual_orders.py::"
            "test_manual_cancel_refuses_a_terminal_target_before_creating_a_cancel_effect",
        ),
    ),
    ManualQualificationScenario(
        "duplicate_submit_and_reload",
        "A repeated confirmation or browser reload returns the original ticket and never duplicates broker contact.",
        (
            "tests/broker/alpaca/clerk/test_manual_order_routes.py::"
            "test_manual_ticket_preview_submit_replay_and_read_are_durable",
            "tests/broker/alpaca/clerk/sqlite/test_manual_orders.py::"
            "test_replay_reuses_one_manual_order_and_changed_content_conflicts",
        ),
    ),
    ManualQualificationScenario(
        "restart_and_exact_reconciliation",
        "An accepted or unknown manual order resumes through exact identity reconciliation without resubmission.",
        (
            "tests/broker/alpaca/clerk/sqlite/test_manual_orders.py::"
            "test_lost_manual_submit_response_remains_queryable_unknown",
            "tests/broker/alpaca/clerk/sqlite/test_reconcile.py::"
            "test_reconcile_account_recovers_an_unknown_manual_order_after_repository_restart",
            "tests/broker/alpaca/clerk/sqlite/test_reconcile.py::"
            "test_reconcile_account_recovers_an_unknown_manual_closed_order",
        ),
    ),
    ManualQualificationScenario(
        "coverage_conflict_recovery",
        "Changed exact evidence blocks the manual subject until the documented coverage recovery completes.",
        (
            "tests/broker/alpaca/clerk/test_trade_evidence.py::"
            "test_manual_rest_recovery_and_later_exact_evidence_stay_subject_scoped",
            "tests/broker/alpaca/clerk/sqlite/test_manual_orders.py::"
            "test_coverage_resolution_completes_a_filled_manual_ticket",
        ),
    ),
    ManualQualificationScenario(
        "ordered_ticket_pause",
        "An unknown leg pauses the remaining ticket so no later broker write can occur without fresh confirmation.",
        (
            "tests/broker/alpaca/clerk/sqlite/test_manual_orders.py::"
            "test_unknown_first_ticket_leg_refuses_later_broker_contact",
            "tests/broker/alpaca/clerk/sqlite/test_manual_orders.py::"
            "test_ticket_cancel_stops_before_later_broker_writes_after_an_unknown_outcome",
        ),
    ),
)

if len({scenario.scenario_id for scenario in MANUAL_QUALIFICATION_SCENARIOS}) != len(
    MANUAL_QUALIFICATION_SCENARIOS
):
    raise RuntimeError("manual qualification scenario identifiers must be unique")
if any(not scenario.test_refs for scenario in MANUAL_QUALIFICATION_SCENARIOS):
    raise RuntimeError("each manual qualification scenario requires focused regressions")


__all__ = ["MANUAL_QUALIFICATION_SCENARIOS", "ManualQualificationScenario"]
