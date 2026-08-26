"""SQLite catalog projection coverage."""

from __future__ import annotations

from typing import Literal

import pytest

from app.broker.alpaca.clerk.sqlite.economic_projection import EconomicSnapshot
from app.broker.alpaca.clerk.sqlite.projection_models import (
    ClerkProjection,
    ClerkScope,
    ProjectedHold,
    ProjectionGuidance,
    RecoveryCapability,
    RecoveryConfirmation,
)
from app.schemas.broker_bots import BotStatusView
from app.schemas.live_runs import BotDutyOutcomeView
from app.services.broker_v2_panel.catalog_projection_service import (
    SqliteCatalogProjectionUnavailable,
    SqliteCatalogRevisionMismatch,
    status_label_for,
)
from app.services.broker_v2_panel.sqlite_panel_adapter import build_sqlite_catalog
from tests.broker.v2panel.fixtures import ACCT, OTHER_SID, SID


def _status(
    *,
    sid: str,
    strategy_key: str = "deployment_validation",
    strategy_label: str | None = "Deployment Validation",
    phase: str = "ON_DUTY",
    running: bool = True,
    desired_state: str = "RUNNING",
    duty_kind: str | None = None,
    mode: Literal["log_only", "dry_run", "trade"] = "log_only",
) -> BotStatusView:
    duty = (
        BotDutyOutcomeView(
            kind=duty_kind, reason_code="X", recorded_at_ms=1, run_id="r1"
        )
        if duty_kind is not None
        else None
    )
    return BotStatusView(
        strategy_instance_id=sid,
        strategy_key=strategy_key,
        strategy_label=strategy_label,
        broker="alpaca",
        symbol="SPY",
        mode=mode,
        quantity=1,
        running=running,
        phase=phase,  # type: ignore[arg-type]
        desired_state=desired_state,  # type: ignore[arg-type]
        active_run_id="r1" if running else None,
        duty_outcome=duty,
        binding_created_at_ms=1,
        last_transition_at_ms=2,
    )


def _economic_snapshot(*, sid: str, control_revision: int = 42) -> EconomicSnapshot:
    return EconomicSnapshot(
        account_id=ACCT,
        strategy_instance_id=sid,
        authority_generation=7,
        control_revision=control_revision,
        session_open_ms=1_700_000_000_000,
        session_close_ms=1_700_023_400_000,
        recent_fills=(),
        fills_today=3,
        exposure={"SPY": 2.0},
        realized_pnl_today=12.5,
        open_pnl=3.25,
        marks_complete=True,
        mark_observed_at_ms={"SPY": 1_700_010_000_000},
        fee_fidelity="reported",
        execution_coverage="complete",
        last_activity_at_ms=1_700_010_000_000,
    )


def _hold(
    *,
    scope: ClerkScope,
    sid: str | None,
    hold_id: str = "hold-1",
    reason_code: str = "EXPOSURE_UNRECONCILED",
) -> ProjectedHold:
    """One ACTIVE hold at its real scope.

    ``scope`` is the field the row-command behaviour turns on, so the fake has
    to be the real frozen dataclass: a bare string cannot distinguish an
    account-wide problem from a bot-scoped one.
    """
    return ProjectedHold(
        hold_id=hold_id,
        scope=scope,
        strategy_instance_id=sid,
        reason_code=reason_code,
        opened_at_ms=1_700_000_000_000,
        evidence_refs=(),
    )


def _projection(
    *,
    sid: str,
    control_revision: int = 42,
    holds: tuple[ProjectedHold, ...] = (),
    recovery_actions: tuple[RecoveryCapability, ...] = (),
) -> ClerkProjection:
    return ClerkProjection(
        account_id=ACCT,
        strategy_instance_id=sid,
        authority_generation=7,
        db_identity_token="db-7",
        authority_health="healthy",
        authority_health_reason=None,
        control_revision=control_revision,
        custody_owner="ACCOUNT_CLERK",
        runs=(),
        commands=(),
        operations=(),
        positions=(),
        holds=holds,
        uncertainties=(),
        latest_reconciliation=None,
        terminal_receipts=(),
        guidance=ProjectionGuidance(
            headline="Account Clerk custody is healthy",
            explanation="SQLite has current custody truth.",
            scope="CUSTODY_SUBJECT",
            impact="Normal Clerk-governed controls remain available.",
            custody_owner="ACCOUNT_CLERK",
            may_create_exposure=True,
            available_safety_actions=(),
            action_required=False,
            next_step="No recovery action is required.",
        ),
        recovery_actions=recovery_actions,
        generated_at_ms=1_700_010_000_000,
    )


def _recovery_capability(
    *,
    primary: bool = True,
    available: bool = True,
) -> RecoveryCapability:
    return RecoveryCapability(
        action_id="cancel_verified_working_orders",
        label="Cancel verified working orders",
        explanation="Cancel the orders the Clerk can still prove it owns.",
        available=available,
        unavailable_reason_code=None if available else "EVIDENCE_UNAVAILABLE",
        unavailable_reason=None if available else "Broker truth is unavailable.",
        scope="CUSTODY_SUBJECT",
        freshness="fresh",
        evidence=(),
        reduction_plan=None,
        confirmation=RecoveryConfirmation(
            title="Cancel working orders?",
            explanation="The runtime stops before new orders are submitted.",
            confirm_label="Cancel orders",
        ),
        next_step="Cancel the orders, then reconcile.",
        concurrency_token="recovery-token",
        execution_ref=None,
        mutation=True,
        primary=primary,
    )


def test_status_label_maps_the_closed_vocabulary() -> None:
    assert status_label_for(_status(sid=SID, running=True)) == "Working"
    assert status_label_for(_status(sid=SID, running=False, phase="OFF_DUTY")) == "Off duty"
    assert status_label_for(_status(sid=SID, phase="RETIRED", running=False)) == "Retired"


def test_an_unclean_exit_is_labelled_distinctly_from_a_deliberate_stop() -> None:
    """S3b: three bots died mid-run and the roster read "Off duty . Flat".

    The audit and `known-gaps.md` both record this as `needs_attention=false`.
    That is wrong -- attention was already true for a crash, and the backend
    already authored crash-specific `status_explanation`. What actually hid
    the failure is the label: the roster renders `status_label`, and a crash
    mapped to the same "Off duty" a clean stop produces.

    The labels come from the shared operator-copy vocabulary rather than new
    strings invented here.
    """
    crashed = _status(sid=SID, running=False, phase="OFF_DUTY", duty_kind="CRASHED")
    unverified = _status(
        sid=SID, running=False, phase="OFF_DUTY", duty_kind="EXITED_UNVERIFIED"
    )
    stopped = _status(sid=SID, running=False, phase="OFF_DUTY", duty_kind="STOPPED")

    assert status_label_for(crashed) == "Crashed"
    assert status_label_for(unverified) == "Exited unverified"
    # A clean stop is still plain "Off duty" -- this must not become alarming.
    assert status_label_for(stopped) == "Off duty"
    # A retired bot keeps its terminal label whatever ended the last run.
    assert (
        status_label_for(
            _status(sid=SID, phase="RETIRED", running=False, duty_kind="CRASHED")
        )
        == "Retired"
    )


def test_sqlite_catalog_uses_config_identity_and_one_economic_rollup() -> None:
    status = _status(
        sid=SID, strategy_key="opening_range_breakout", mode="trade"
    ).model_copy(update={"strategy_label": "Opening Range Breakout — Paper"})
    catalog = build_sqlite_catalog(
        [status],
        projections={SID: _projection(sid=SID)},
        economic_rollups={SID: _economic_snapshot(sid=SID)},
        account_id=ACCT,
    )

    assert len(catalog) == 1
    row = catalog[0]
    assert row.strategy_key == "opening_range_breakout"
    assert row.strategy_label == "Opening Range Breakout — Paper"
    assert row.exposure == {"SPY": 2.0}
    assert row.fills_today == 3
    assert row.realized_pnl_today == pytest.approx(12.5, abs=1e-6)
    assert row.open_pnl == pytest.approx(3.25, abs=1e-6)
    assert row.last_activity_at_ms == 1_700_010_000_000


def test_sqlite_catalog_refuses_a_registered_bot_without_immutable_config() -> None:
    with pytest.raises(SqliteCatalogProjectionUnavailable, match="immutable SQLite configuration"):
        build_sqlite_catalog(
            [_status(sid=SID, strategy_key="unknown", mode="trade")],
            projections={SID: _projection(sid=SID)},
            economic_rollups={SID: _economic_snapshot(sid=SID)},
            account_id=ACCT,
        )


def test_sqlite_catalog_refuses_a_registered_bot_without_config_display_name() -> None:
    with pytest.raises(SqliteCatalogProjectionUnavailable, match="immutable SQLite configuration"):
        build_sqlite_catalog(
            [
                _status(
                    sid=SID,
                    strategy_key="opening_range_breakout",
                    strategy_label=None,
                    mode="trade",
                )
            ],
            projections={SID: _projection(sid=SID)},
            economic_rollups={SID: _economic_snapshot(sid=SID)},
            account_id=ACCT,
        )


def test_sqlite_catalog_refuses_economics_spanning_authority_revisions() -> None:
    with pytest.raises(SqliteCatalogProjectionUnavailable, match="multiple authority revisions"):
        build_sqlite_catalog(
            [
                _status(sid=SID, strategy_key="opening_range_breakout", mode="trade"),
                _status(sid=OTHER_SID, strategy_key="ema_crossover_signal", mode="trade"),
            ],
            projections={
                SID: _projection(sid=SID, control_revision=42),
                OTHER_SID: _projection(sid=OTHER_SID, control_revision=43),
            },
            economic_rollups={
                SID: _economic_snapshot(sid=SID, control_revision=42),
                OTHER_SID: _economic_snapshot(sid=OTHER_SID, control_revision=43),
            },
            account_id=ACCT,
        )


def test_sqlite_catalog_refuses_custody_and_economics_from_different_revisions() -> None:
    with pytest.raises(SqliteCatalogRevisionMismatch, match="do not share one authority revision"):
        build_sqlite_catalog(
            [_status(sid=SID, strategy_key="opening_range_breakout", mode="trade")],
            projections={SID: _projection(sid=SID, control_revision=41)},
            economic_rollups={SID: _economic_snapshot(sid=SID, control_revision=42)},
            account_id=ACCT,
        )


# ── S2/S4 (#1778): the roster's per-row recovery command ─────────────────────
# `BotCatalogView.row_action` existed on the wire with no producer -- the
# SQLite adaptation hardcoded `None` -- so an attention row offered the
# operator no command at all. The row now carries its primary recovery
# capability, built by the same `_panel_action` the panel uses so the
# revision/token guard *and* the typed confirmation travel with it.


def test_an_attention_row_carries_its_primary_recovery_command() -> None:
    catalog = build_sqlite_catalog(
        [_status(sid=SID)],
        projections={
            SID: _projection(
                sid=SID,
                holds=(_hold(scope="CUSTODY_SUBJECT", sid=SID),),
                recovery_actions=(_recovery_capability(),),
            )
        },
        economic_rollups={SID: _economic_snapshot(sid=SID)},
        account_id=ACCT,
    )

    row = catalog[0]
    assert row.needs_attention is True
    assert row.row_action is not None
    assert row.row_action.action_id == "cancel_verified_working_orders"
    # The guard contract is the panel's, not a roster-local invention.
    assert row.row_action.revision == 42
    assert row.row_action.concurrency_token == "recovery-token"
    # Promoting the command must not drop its typed confirmation.
    assert row.row_action.confirmation is not None
    assert row.row_action.confirmation.confirm_label == "Cancel orders"


def test_an_unavailable_recovery_command_is_offered_with_its_blocker() -> None:
    catalog = build_sqlite_catalog(
        [_status(sid=SID)],
        projections={
            SID: _projection(
                sid=SID,
                holds=(_hold(scope="CUSTODY_SUBJECT", sid=SID),),
                recovery_actions=(_recovery_capability(available=False),),
            )
        },
        economic_rollups={SID: _economic_snapshot(sid=SID)},
        account_id=ACCT,
    )

    row_action = catalog[0].row_action
    assert row_action is not None
    # Honest rather than hidden: the row shows why it cannot act.
    assert row_action.enabled is False
    assert row_action.blockers


def test_a_healthy_row_carries_no_recovery_command() -> None:
    """Contract regression the other way.

    A row with nothing wrong must stay a plain roster row. Recovery commands
    are for rows that need attention; offering one everywhere would make the
    rail alarming and the command meaningless.
    """
    catalog = build_sqlite_catalog(
        [_status(sid=SID)],
        projections={SID: _projection(sid=SID, recovery_actions=(_recovery_capability(),))},
        economic_rollups={SID: _economic_snapshot(sid=SID)},
        account_id=ACCT,
    )

    assert catalog[0].needs_attention is False
    assert catalog[0].row_action is None


def test_an_attention_row_without_a_primary_capability_offers_nothing() -> None:
    catalog = build_sqlite_catalog(
        [_status(sid=SID)],
        projections={
            SID: _projection(
                sid=SID,
                holds=(_hold(scope="CUSTODY_SUBJECT", sid=SID),),
                recovery_actions=(_recovery_capability(primary=False),),
            )
        },
        economic_rollups={SID: _economic_snapshot(sid=SID)},
        account_id=ACCT,
    )

    assert catalog[0].row_action is None


def test_an_account_scoped_hold_puts_no_per_bot_command_on_any_row() -> None:
    """An account-scoped problem has an account-scoped cure.

    ``ClerkSqliteProjectionReader._holds`` folds every ``ACCOUNT_CLERK`` row
    into *each* bot's snapshot, so one account-wide hold marks the whole fleet
    ``needs_attention``. Deriving the row command from that fold would hand N
    operators N per-bot mutation buttons for one problem -- the same fan-out
    defect family as the account-wide entry freeze this PRD removes.

    Attention itself stays: the rows are genuinely affected, and saying so is
    honest. What must not appear is the button.
    """
    account_hold = _hold(scope="ACCOUNT_CLERK", sid=None, hold_id="hold-account")
    catalog = build_sqlite_catalog(
        [_status(sid=SID), _status(sid=OTHER_SID, strategy_key="ema_crossover_signal")],
        projections={
            SID: _projection(
                sid=SID,
                holds=(account_hold,),
                recovery_actions=(_recovery_capability(),),
            ),
            OTHER_SID: _projection(
                sid=OTHER_SID,
                holds=(account_hold,),
                recovery_actions=(_recovery_capability(),),
            ),
        },
        economic_rollups={
            SID: _economic_snapshot(sid=SID),
            OTHER_SID: _economic_snapshot(sid=OTHER_SID),
        },
        account_id=ACCT,
    )

    assert len(catalog) == 2
    assert [row.needs_attention for row in catalog] == [True, True]
    assert [row.row_action for row in catalog] == [None, None]


def test_a_bot_scoped_hold_still_commands_only_its_own_row() -> None:
    """The other half of the scope contract.

    Narrowing the derivation to ``CUSTODY_SUBJECT`` must not mute the case it
    exists for: the bot that actually holds the stranded exposure keeps its
    command, and its unaffected sibling gets none.
    """
    catalog = build_sqlite_catalog(
        [_status(sid=SID), _status(sid=OTHER_SID, strategy_key="ema_crossover_signal")],
        projections={
            SID: _projection(
                sid=SID,
                holds=(_hold(scope="CUSTODY_SUBJECT", sid=SID),),
                recovery_actions=(_recovery_capability(),),
            ),
            OTHER_SID: _projection(
                sid=OTHER_SID,
                recovery_actions=(_recovery_capability(),),
            ),
        },
        economic_rollups={
            SID: _economic_snapshot(sid=SID),
            OTHER_SID: _economic_snapshot(sid=OTHER_SID),
        },
        account_id=ACCT,
    )

    held, sibling = catalog
    assert held.row_action is not None
    assert held.row_action.action_id == "cancel_verified_working_orders"
    assert sibling.needs_attention is False
    assert sibling.row_action is None
