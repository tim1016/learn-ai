"""Neutral domain types for the account-custody synthetic rehearsal."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType


class SyntheticAbortCause(StrEnum):
    """Stable failure causes shared by qualification contracts and execution."""

    NONE = "NONE"
    FEED_REPLAY_FAILURE = "FEED_REPLAY_FAILURE"
    DUPLICATE_ECONOMIC_INTENT = "DUPLICATE_ECONOMIC_INTENT"
    BROKER_MUTATION_WITHOUT_FINALIZED_INTENT = "BROKER_MUTATION_WITHOUT_FINALIZED_INTENT"
    MIXED_AUTHORITY_WRITERS = "MIXED_AUTHORITY_WRITERS"
    UNEXPLAINED_CUSTODY_DRIFT = "UNEXPLAINED_CUSTODY_DRIFT"
    CUSTODY_STATE_REGRESSION = "CUSTODY_STATE_REGRESSION"
    FABRICATED_TERMINAL_RESULT = "FABRICATED_TERMINAL_RESULT"
    STALE_OR_UNKNOWN_EVIDENCE_AUTHORIZED_EXPOSURE = "STALE_OR_UNKNOWN_EVIDENCE_AUTHORIZED_EXPOSURE"
    CORRUPT_MISSING_OR_SUBSTITUTED_AUTHORITY = "CORRUPT_MISSING_OR_SUBSTITUTED_AUTHORITY"
    RECOVERY_HASH_IDENTITY_FAILURE = "RECOVERY_HASH_IDENTITY_FAILURE"
    UI_EXECUTION_POLICY_MISMATCH = "UI_EXECUTION_POLICY_MISMATCH"
    INFRASTRUCTURE_OR_TOOLING_FAILURE = "INFRASTRUCTURE_OR_TOOLING_FAILURE"


class SyntheticScenarioId(StrEnum):
    """Stable wire identifiers for every synthetic rehearsal scenario."""

    POLYGON_REPLAY_CLOSED_MINUTES = "polygon_replay_closed_minutes"
    FEED_ONE_PRINT_STALL = "feed_one_print_stall"
    FEED_NEVER_FIRST_BAR = "feed_never_first_bar"
    LOST_SUBMIT_EXACT_IDENTITY = "lost_submit_exact_identity"
    DUPLICATE_TRADE_UPDATE = "duplicate_trade_update"
    CANCEL_FILL_RACE = "cancel_fill_race"
    LOST_CANCEL_RETAINS_CUSTODY = "lost_cancel_retains_custody"
    TRADE_UPDATE_GAP_RECONCILIATION = "trade_update_gap_reconciliation"
    BOT_UNCERTAINTY_ISOLATION = "bot_uncertainty_isolation"
    UNCLASSIFIED_UNCERTAINTY_ACCOUNT_CLERK = "unclassified_uncertainty_account_clerk"
    RESTART_IN_FLIGHT_WORK = "restart_in_flight_work"
    EVIDENCE_CONTROLS_READ_ONLY = "evidence_controls_read_only"
    RECOVERY_CEREMONY = "recovery_ceremony"


class SyntheticScenarioCategory(StrEnum):
    """Stable scenario groups used by qualification execution."""

    FEED = "feed"
    CUSTODY = "custody"
    UI = "ui"
    RECOVERY = "recovery"


_ABORT_CAUSE_BY_SCENARIO: Mapping[SyntheticScenarioId, SyntheticAbortCause] = MappingProxyType(
    {
        SyntheticScenarioId.POLYGON_REPLAY_CLOSED_MINUTES: SyntheticAbortCause.FEED_REPLAY_FAILURE,
        SyntheticScenarioId.FEED_ONE_PRINT_STALL: SyntheticAbortCause.FEED_REPLAY_FAILURE,
        SyntheticScenarioId.FEED_NEVER_FIRST_BAR: SyntheticAbortCause.FEED_REPLAY_FAILURE,
        SyntheticScenarioId.LOST_SUBMIT_EXACT_IDENTITY: SyntheticAbortCause.BROKER_MUTATION_WITHOUT_FINALIZED_INTENT,
        SyntheticScenarioId.DUPLICATE_TRADE_UPDATE: SyntheticAbortCause.DUPLICATE_ECONOMIC_INTENT,
        SyntheticScenarioId.CANCEL_FILL_RACE: SyntheticAbortCause.CUSTODY_STATE_REGRESSION,
        SyntheticScenarioId.LOST_CANCEL_RETAINS_CUSTODY: SyntheticAbortCause.FABRICATED_TERMINAL_RESULT,
        SyntheticScenarioId.TRADE_UPDATE_GAP_RECONCILIATION: SyntheticAbortCause.STALE_OR_UNKNOWN_EVIDENCE_AUTHORIZED_EXPOSURE,
        SyntheticScenarioId.BOT_UNCERTAINTY_ISOLATION: SyntheticAbortCause.STALE_OR_UNKNOWN_EVIDENCE_AUTHORIZED_EXPOSURE,
        SyntheticScenarioId.UNCLASSIFIED_UNCERTAINTY_ACCOUNT_CLERK: SyntheticAbortCause.STALE_OR_UNKNOWN_EVIDENCE_AUTHORIZED_EXPOSURE,
        SyntheticScenarioId.RESTART_IN_FLIGHT_WORK: SyntheticAbortCause.CORRUPT_MISSING_OR_SUBSTITUTED_AUTHORITY,
        SyntheticScenarioId.EVIDENCE_CONTROLS_READ_ONLY: SyntheticAbortCause.UI_EXECUTION_POLICY_MISMATCH,
        SyntheticScenarioId.RECOVERY_CEREMONY: SyntheticAbortCause.RECOVERY_HASH_IDENTITY_FAILURE,
    }
)
if set(_ABORT_CAUSE_BY_SCENARIO) != set(SyntheticScenarioId):
    raise RuntimeError("synthetic abort policy must cover every scenario ID")


def synthetic_abort_cause_for_scenario(
    scenario_id: SyntheticScenarioId,
) -> SyntheticAbortCause:
    """Return the predeclared finding represented by a failed scenario."""

    return _ABORT_CAUSE_BY_SCENARIO[scenario_id]


__all__ = [
    "SyntheticAbortCause",
    "SyntheticScenarioCategory",
    "SyntheticScenarioId",
    "synthetic_abort_cause_for_scenario",
]
